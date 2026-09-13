#!/usr/bin/env python3
"""swserver control panel.

Single-process supervisor for the Stormworks dedicated server:
  - Steam authentication (cached token, credentials, or QR via Steam mobile app)
  - game server download / update via SteamCMD
  - start / stop / restart of the wine-hosted server (with Xvfb)
  - tiny dependency-free web UI (port PANEL_PORT, default 8080)

All state is kept simple and on disk:
  /home/steam/panel.log        combined log (steamcmd + wine output)
  /home/steam/panel_state.json last successful update, etc.
"""

import base64
import html
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HOME = os.environ.get("HOME", "/home/steam")
STEAMDIR = os.path.join(HOME, "steamcmd")
STEAMCMD = os.path.join(STEAMDIR, "steamcmd.sh")
SW_DIR = os.path.join(HOME, "sw")
SDK_DIR = os.path.join(HOME, "steamworks_sdk")
WINEPREFIX = os.environ.get("WINEPREFIX", os.path.join(HOME, "wine-data", "prefix"))
SW_SETTINGS_DIR = os.path.join(
    WINEPREFIX, "drive_c", "users", "steam", "AppData", "Roaming", "Stormworks")
SERVER_CONFIG = os.path.join(SW_SETTINGS_DIR, "server_config.xml")
LOG_PATH = os.path.join(HOME, "panel.log")
STATE_PATH = os.path.join(HOME, "panel_state.json")
QR_PATH = "/tmp/steam_auth_qr.png"
GAME_APP_ID = int(os.environ.get("SW_APP_ID", "573090"))
SDK_APP_ID = 1007
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")

SERVER_EXE_CANDIDATES = [
    "server64.exe",
    "Server/server64.exe",
    "Tools/server64.exe",
    "Tools/Server/server64.exe",
    "Stormworks/server64.exe",
    "Bin/server64.exe",
    "Build and Rescue/server64.exe",
    "Build and Rescue/Server/server64.exe",
    "Build and Rescue/Dedicated Server/server64.exe",
]

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PW_RE = re.compile(r"password:\s*$", re.I)
GUARD_RE = re.compile(r"(enter the code|steam guard|authenticator|mobile app|code:)", re.I)
QR_RE = re.compile(r"data for qr authentication", re.I)
CONFIRM_RE = re.compile(r"waiting for confirmation", re.I)
LOGGED_IN_RE = re.compile(
    r"(Logged in OK|Login Success|Waiting for user info\.\.\.OK|Logging in user.*OK)", re.I)
FAIL_RE = re.compile(r"(ERROR \(|Login Failed|Login failure|Invalid Password|Too many login attempts)", re.I)


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass


def find_wine_bin():
    for cand in ("wine", "wine64"):
        p = shutil.which(cand)
        if p:
            return p
    for cand in ("/usr/lib/wine/wine64", "/usr/bin/wine64"):
        if os.path.exists(cand):
            return cand
    return "wine"


def tail_log(nbytes=16384, lines=60):
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return "".join(f.read().decode(errors="replace").splitlines(True)[-lines:])
    except OSError:
        return "(no log yet)"


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# SteamCMD interactive login (pty-driven; verified against steamcmd 1788292693)
# ---------------------------------------------------------------------------

class SteamSession:
    """Drives one interactive `login <user>` session inside a pty.

    Flow: Steam> prompt -> login <user> -> password: -> <pass> ->
    Steam Guard prompt -> <guard code or blank Enter to request QR> ->
    "data for qr authentication:" (JSON) -> user scans in Steam app ->
    "Logged in OK" on the SAME process.
    """

    def __init__(self, username, password, guard=None):
        self.username = username
        self.password = password
        self.guard = guard or ""
        self.state = "starting"       # starting|running|awaiting_scan|authenticated|failed
        self.message = ""
        self.qr_ready = False
        self._fd = None
        self._pid = None

    def run(self, timeout=420):
        os.makedirs(os.path.join(HOME, "Steam", "logs"), exist_ok=True)
        self._pid, self._fd = pty.fork()
        if self._pid == 0:  # child
            try:
                os.chdir(STEAMDIR)
                os.execvp(STEAMCMD, [STEAMCMD])
            except Exception:
                os._exit(127)
        self.state = "running"
        self.message = "Connecting to Steam..."
        log("starting interactive steamcmd login for %s" % self.username)
        buf = ""
        sent_login = False
        last_logged = ""
        last_log_time = 0.0
        sent_password = False
        sent_guard = False
        confirm_seen = False
        qr_text = ""
        collecting_qr = False
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                try:
                    r, _, _ = select.select([self._fd], [], [], 0.5)
                    if not r:
                        continue
                    data = os.read(self._fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunk = ANSI_RE.sub("", data.decode(errors="replace")).replace("\r", "")
                buf += chunk
                flat = buf[-4000:]

                now = time.time()
                throttle = 5 if not sent_guard else 1
                if now - last_log_time > throttle:
                    lines = [l.strip() for l in chunk.splitlines() if l.strip()]
                    newest = lines[-1] if lines else ""
                    if newest and newest != last_logged:
                        log("steamcmd: " + newest[:200])
                        last_logged = newest
                        last_log_time = now

                if self.state == "failed":
                    break

                if not sent_login and re.search(r"Steam>\s*$", flat):
                    os.write(self._fd, ("login %s\n" % self.username).encode())
                    sent_login = True
                    self.message = "Login request sent..."
                    buf = ""
                    continue

                if not sent_password and PW_RE.search(flat):
                    os.write(self._fd, (self.password + "\n").encode())
                    sent_password = True
                    self.message = "Password sent, waiting for Steam Guard prompt..."
                    log("password prompt reached; password sent")
                    buf = ""
                    continue

                if sent_password and not sent_guard and not collecting_qr \
                        and GUARD_RE.search(flat) and not QR_RE.search(flat):
                    if self.guard:
                        os.write(self._fd, (self.guard + "\n").encode())
                        self.message = "Guard code sent, waiting for login result..."
                        log("guard prompt reached; guard code sent")
                    else:
                        os.write(self._fd, b"\n")
                        self.message = "Requested QR authentication - check the QR code..."
                        log("guard prompt reached; sent Enter to request QR")
                    sent_guard = True
                    buf = ""
                    continue

                if CONFIRM_RE.search(flat) and not confirm_seen:
                    confirm_seen = True
                    self.message = ("Check your Steam Mobile app for a sign-in "
                                    "notification and tap Approve (or scan the QR "
                                    "code above if one is shown).")
                    log("steamcmd awaiting mobile confirmation; raw tail:\n" + flat[-800:])

                if QR_RE.search(flat) and not self.qr_ready:
                    collecting_qr = True
                    qr_start = flat.find("{")
                    if qr_start >= 0:
                        qr_text = flat[qr_start:]
                        if qr_text.count("{") == qr_text.count("}") and qr_text.count("}") >= 1:
                            if self._render_qr(qr_text):
                                break
                    else:
                        qr_text = ""
                    buf = ""
                    continue

                if collecting_qr and not self.qr_ready:
                    qr_text += chunk
                    if qr_text.count("{") >= 1 and qr_text.count("{") == qr_text.count("}"):
                        if self._render_qr(qr_text):
                            break
                    continue

                if LOGGED_IN_RE.search(flat):
                    self.state = "authenticated"
                    self.message = "Logged in to Steam."
                    log("Steam login succeeded for %s" % self.username)
                    try:
                        os.write(self._fd, b"quit\n")
                        time.sleep(1)
                    except OSError:
                        pass
                    break

                m = FAIL_RE.search(flat)
                if m and (sent_password or "login" in flat.lower()):
                    fail_line = m.group(0)
                    for ln in flat.splitlines():
                        if FAIL_RE.search(ln):
                            fail_line = ln.strip()
                            break
                    self.state = "failed"
                    self.message = "Login failed: %s (check username/password/guard code)" % fail_line
                    log("Steam login FAILED for %s: %s" % (self.username, fail_line))
                    break

                # steamcmd sometimes signals a completed login (esp. via cached
                # token or mobile approval) simply by returning to the prompt
                # with an OK and no error text
                if sent_login and sent_guard and flat.rstrip().endswith("Steam>"):
                    self.state = "authenticated"
                    self.message = "Logged in to Steam."
                    log("login completed (prompt returned without error)")
                    try:
                        os.write(self._fd, b"quit\n")
                        time.sleep(1)
                    except OSError:
                        pass
                    break
            else:
                if self.state not in ("authenticated", "failed"):
                    self.state = "failed"
                    self.message = "Timed out waiting for Steam authentication."
                    log("login timeout; last steamcmd output:\n" + flat[-1000:])
        finally:
            try:
                os.kill(self._pid, signal.SIGKILL)
                os.waitpid(self._pid, 0)
            except (OSError, ChildProcessError):
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
        return self.state == "authenticated"

    def _render_qr(self, raw):
        try:
            data = json.loads(raw)
            payload = data.get("url") or raw
        except ValueError:
            payload = raw
        if os.path.exists(QR_PATH):
            os.remove(QR_PATH)
        try:
            subprocess.run(
                ["qrencode", "-o", QR_PATH, "-t", "PNG", "-s", "8", "-m", "2", payload],
                check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as e:
            self.state = "failed"
            self.message = "Failed to render QR code: %s" % e
            return True  # stop the loop
        self.qr_ready = True
        self.state = "awaiting_scan"
        self.message = "Scan the QR code with the Steam Mobile app (QR tab), then confirm."
        log("QR code generated; waiting for user to scan")
        return True


def quick_login_check(username):
    """Try a non-interactive cached-token login. Returns True/False."""
    if not username:
        return False
    try:
        p = subprocess.run(
            [STEAMCMD, "+login", username, "+quit"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=60)
        out = ANSI_RE.sub("", p.stdout.decode(errors="replace"))
        return bool(LOGGED_IN_RE.search(out)) and not FAIL_RE.search(out)
    except subprocess.TimeoutExpired:
        return False


def run_steamcmd_script(commands, logfile_tag="steamcmd"):
    """Run steamcmd with a runscript file (keeps creds out of argv). Returns (ok, output)."""
    rs = "/tmp/runscript_%d.txt" % os.getpid()
    with open(rs, "w") as f:
        for cmd in commands:
            f.write(cmd + "\n")
        f.write("quit\n")
    try:
        with open(LOG_PATH, "ab") as lf:
            lf.write(("\n===== %s =====\n" % logfile_tag).encode())
            lf.flush()
            p = subprocess.run(
                [STEAMCMD, "+runscript", rs],
                stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                timeout=3600, cwd=STEAMDIR)
        out = tail_log(4000, 40)
        ok = p.returncode == 0 and "ERROR!" not in out
        return ok, out
    except subprocess.TimeoutExpired:
        return False, "steamcmd timed out"
    finally:
        try:
            os.remove(rs)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = ""               # human-readable current operation
        self.session = None          # active SteamSession
        self.auth_state = "unknown"  # unknown|authenticated|awaiting_scan|failed|none
        self.auth_message = ""
        self.auth_username = os.environ.get("STEAM_USERNAME", "")
        self.xvfb = None
        self.server = None           # subprocess handle for wine server
        self.server_started = 0
        self.cmd_lock = threading.Lock()

    # ---- auth -------------------------------------------------------------

    def try_cached_login(self):
        if self.auth_username:
            with self.cmd_lock:
                if quick_login_check(self.auth_username):
                    self.auth_state = "authenticated"
                    self.auth_message = "Logged in with cached Steam credentials."
                    log("cached login OK for " + self.auth_username)
                    return True
        return False

    def start_login(self, username, password, guard):
        with self.lock:
            if self.session and self.session.state in ("starting", "running", "awaiting_scan"):
                return "login already in progress"
        self.auth_username = username
        session = SteamSession(username, password, guard)
        self.session = session
        self.auth_state = "logging_in"
        self.auth_message = "Starting Steam login..."

        def work():
            with self.cmd_lock:
                ok = session.run()
            final = "authenticated" if ok else "failed"
            self.auth_state = final
            self.auth_message = session.message or ("Logged in." if ok else "Login failed.")
            if ok:
                threading.Thread(target=self.ensure_install, daemon=True).start()

        def mirror():
            while session.state in ("starting", "running", "awaiting_scan"):
                self.auth_state = "awaiting_scan" if session.state == "awaiting_scan" else "logging_in"
                self.auth_message = session.message
                time.sleep(1)
        threading.Thread(target=mirror, daemon=True).start()
        threading.Thread(target=work, daemon=True).start()
        return None

    # ---- install / update ---------------------------------------------------

    def server_exe(self):
        for c in SERVER_EXE_CANDIDATES:
            p = os.path.join(SW_DIR, c)
            if os.path.isfile(p):
                return p
        return None

    def installed(self):
        return self.server_exe() is not None

    def _login_cmds(self):
        # relies on the interactive login persisting login tokens for this user
        return ["login %s" % self.auth_username] if self.auth_username else ["login anonymous"]

    def ensure_install(self):
        with self.cmd_lock:
            self.busy = "downloading game files"
            try:
                return self._ensure_install()
            finally:
                self.busy = ""

    def _ensure_install(self):
        if True:
            log("installing/updating Stormworks (app %d)" % GAME_APP_ID)
            ok1, _ = run_steamcmd_script(
                self._login_cmds() +
                ["force_install_dir " + SDK_DIR, "app_update %d validate" % SDK_APP_ID],
                "steamworks sdk")
            ok2, out = run_steamcmd_script(
                self._login_cmds() +
                ["@sSteamCmdForcePlatformType windows",
                 "force_install_dir " + SW_DIR,
                 "app_update %d validate" % GAME_APP_ID],
                "stormworks install")
            if ok2:
                self._copy_sdk_dlls()
                st = load_state()
                st["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_state(st)
                log("install/update finished OK")
                # auto-start on first successful install if nothing running
                if not self.server:
                    self.start_server()
            else:
                log("install/update FAILED:\n" + out[-1500:])
            return ok2

    def update_now(self):
        was_running = bool(self.server)
        if was_running:
            self.stop_server()

        def work():
            self.ensure_install()
            if was_running:
                self.start_server()
        threading.Thread(target=work, daemon=True).start()

    def _copy_sdk_dlls(self):
        try:
            for f in os.listdir(SDK_DIR):
                if f.endswith("64.dll"):
                    shutil.copy(os.path.join(SDK_DIR, f), SW_DIR)
        except OSError:
            pass

    # ---- server process -----------------------------------------------------

    def start_server(self):
        with self.lock:
            if self.server:
                return "server already running"
            exe = self.server_exe()
            if not exe:
                return "server not installed yet"
        os.makedirs(WINEPREFIX, exist_ok=True)
        try:
            subprocess.run(["wineboot", "--init"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=self._wine_env())
        except FileNotFoundError:
            pass  # wine64-only images have no wineboot wrapper; wine creates the prefix itself
        if not self.xvfb:
            self.xvfb = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1024x768x16"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
        args = [find_wine_bin(), exe]
        gslt = os.environ.get("STEAM_GSLT", "")
        if gslt:
            os.makedirs(os.path.join(SW_DIR, "config"), exist_ok=True)
            with open(os.path.join(SW_DIR, "config", "server.cfg"), "w") as f:
                f.write("// managed by panel\nsv_setsteamaccount \"%s\"\n" % gslt)
        lf = open(LOG_PATH, "ab")
        lf.write(("\n===== server start %s =====\n" % time.strftime("%F %T")).encode())
        self.server = subprocess.Popen(
            args, cwd=SW_DIR, stdout=lf, stderr=subprocess.STDOUT,
            env=self._wine_env(), start_new_session=True)
        self.server_started = time.time()
        log("server started pid=%d exe=%s" % (self.server.pid, exe))
        return None

    def stop_server(self):
        with self.lock:
            proc = self.server
        if not proc:
            return None
        log("stopping server pid=%d" % proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
        self.server = None
        log("server stopped")
        return None

    def restart_server(self):
        def work():
            self.stop_server()
            self.start_server()
        threading.Thread(target=work, daemon=True).start()

    def _wine_env(self):
        env = dict(os.environ)
        env["WINEPREFIX"] = WINEPREFIX
        env["DISPLAY"] = ":99"
        env.setdefault("WINEDEBUG", "-all")
        return env

    # ---- server config / world data -----------------------------------------

    def get_config(self):
        try:
            with open(SERVER_CONFIG, "r", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def save_config(self, content, purge=False):
        if "<server_data" not in content:
            return "refusing to save: does not look like server_config.xml"
        with self.lock:
            was_running = bool(self.server)
        self.stop_server()
        if purge:
            # Stormworks loads existing world data over some config settings;
            # wiping the saves dir forces a fresh world with the new config
            try:
                shutil.rmtree(SW_SETTINGS_DIR)
                log("purged world data for config reload")
            except OSError as e:
                log("purge failed: %s" % e)
        os.makedirs(os.path.dirname(SERVER_CONFIG), exist_ok=True)
        tmp = SERVER_CONFIG + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, SERVER_CONFIG)
        log("server_config.xml updated via panel%s" % (" (world purged)" if purge else ""))
        self.start_server()
        return None

    def reset_world(self):
        with self.lock:
            was_running = bool(self.server)
        self.stop_server()
        try:
            shutil.rmtree(SW_SETTINGS_DIR)
            log("wiped saves, working_server and server_config.xml - "
                "defaults regenerate on next start")
        except OSError as e:
            return str(e)
        if was_running:
            self.start_server()
        return None

    # ---- status -------------------------------------------------------------

    def status(self):
        st = load_state()
        server = "stopped"
        uptime = 0
        pid = None
        if self.server:
            rc = self.server.poll()
            if rc is None:
                server = "running"
                pid = self.server.pid
                uptime = int(time.time() - self.server_started)
            else:
                self.server = None
                server = "exited (%s)" % rc
        return {
            "auth_state": self.auth_state,
            "auth_message": self.auth_message,
            "username": self.auth_username,
            "qr_ready": bool(self.session and self.session.qr_ready),
            "installed": self.installed(),
            "server": server,
            "pid": pid,
            "uptime": uptime,
            "busy": self.busy,
            "last_update": st.get("last_update", "never"),
            "app_id": GAME_APP_ID,
        }


SUP = Supervisor()


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stormworks Server</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#14181d;color:#dde3ea;margin:0;padding:1rem}
 h1{font-size:1.3rem} h2{font-size:1rem;color:#8fb7d8;margin-bottom:.4rem}
 .cards{display:flex;flex-wrap:wrap;gap:1rem}
 .card{background:#1d242b;border-radius:8px;padding:1rem;min-width:300px;flex:1}
 .badge{padding:2px 10px;border-radius:12px;font-size:.85rem}
 .ok{background:#1d5c2e}.bad{background:#6b1f1f}.warn{background:#6b5a1f}
 button{background:#2d6cdf;color:#fff;border:0;border-radius:6px;padding:8px 14px;margin:4px 4px 4px 0;cursor:pointer}
 button:disabled{background:#444;cursor:default}
 input{background:#0e1114;color:#eee;border:1px solid #333;border-radius:4px;padding:6px;margin:3px 0;width:220px}
 pre{background:#0e1114;padding:8px;border-radius:6px;overflow:auto;max-height:400px;font-size:.75rem;white-space:pre-wrap}
 img{background:#fff;padding:8px;border-radius:6px}
 .muted{color:#8899aa;font-size:.85rem}
</style></head><body>
<h1>⚓ Stormworks Server</h1>
<div class="cards">
 <div class="card"><h2>Status</h2><div id="status">loading...</div>
  <div>
   <button onclick="act('server/start')">Start</button>
   <button onclick="act('server/stop')">Stop</button>
   <button onclick="act('server/restart')">Restart</button>
   <button onclick="act('update')">Check for updates</button>
  </div>
 </div>
 <div class="card"><h2>Steam login</h2><div id="authmsg" class="muted"></div>
  <div id="loginform">
   <input id="user" placeholder="Steam username" value="USERNAME_PLACEHOLDER"><br>
   <input id="pass" type="password" placeholder="Password"><br>
   <input id="guard" placeholder="Guard code (optional - blank = QR)">
   <button onclick="login()">Log in</button>
  </div>
  <div id="qrbox" style="display:none"><img src="/api/auth/qr.png" width="260"><br>
   <span class="muted">Open Steam app → QR code tab → scan</span></div>
 </div>
 <div class="card"><h2>Log</h2><button onclick="loadLog(true)">refresh</button><pre id="log"></pre></div>
</div>
<div class="cards" style="margin-top:1rem">
 <div class="card" style="flex-basis:100%"><h2>Server config (server_config.xml)</h2>
  <textarea id="cfg" style="width:100%;height:300px;background:#0e1114;color:#cde;border:1px solid #333;border-radius:6px;font-family:monospace;font-size:.75rem;padding:8px" spellcheck="false"></textarea><br>
  <button onclick="loadCfg()">Reload from server</button>
  <button onclick="saveCfg()">Save &amp; restart server</button>
  <label style="font-size:.85rem"><input type="checkbox" id="purge" checked> purge world data on save</label>
  <button style="background:#8b2222" onclick="wipeWorld()">Wipe world &amp; config (regenerate)</button>
  <span class="muted">Wiping deletes saves + working_server + config; the server recreates defaults on next start.</span>
 </div>
</div>
<script>
function badge(s){let c='warn';if(/running|authenticated/.test(s))c='ok';if(/stopped|failed|none/.test(s))c='bad';
 return '<span class="badge '+c+'">'+s+'</span>'}
async function poll(){
 const r=await fetch('/api/status');const s=await r.json();
 document.getElementById('status').innerHTML =
  'Server: '+badge(s.server)+(s.pid?' (pid '+s.pid+')':'')+
  '<br>Uptime: '+(s.uptime?Math.floor(s.uptime/60)+'m '+(s.uptime%60)+'s':'-')+
  '<br>Game files: '+badge(s.installed?'installed':'not installed')+
  '<br>Last update: '+s.last_update+
  (s.busy?'<br><span class="muted">'+s.busy+'...</span>':'');
 document.getElementById('authmsg').textContent = s.auth_message||'';
 if(s.qr_ready)document.getElementById('qrbox').style.display='block';
 if(s.auth_state==='authenticated'){document.getElementById('loginform').style.display='none';
  document.getElementById('qrbox').style.display='none';}
 document.title=(/running/.test(s.server)?'▶ ':'⏸ ')+'Stormworks';
}
async function loadLog(nocache){
 const r=await fetch('/api/log?_='+(nocache?Date.now():0));const t=await r.text();
 const el=document.getElementById('log');el.textContent=t;el.scrollTop=el.scrollHeight;}
async function act(a){await fetch('/api/'+a,{method:'POST'});poll();}
async function loadCfg(){const r=await fetch('/api/config');document.getElementById('cfg').value=await r.text();}
async function saveCfg(){
 const purge=document.getElementById('purge').checked;
 if(!confirm(purge?'Save config, PURGE world data and restart?':'Save config and restart the server?'))return;
 const r=await fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'content='+encodeURIComponent(document.getElementById('cfg').value)+'&purge='+purge});
 const t=await r.text(); if(t)alert(t); poll();}
async function wipeWorld(){
 if(!confirm('Delete ALL world data, saves and server config? This cannot be undone. Server restarts with defaults.'))return;
 const r=await fetch('/api/server/reset-world',{method:'POST'});
 const t=await r.text(); if(t)alert(t); loadCfg(); poll();}
async function login(){
 const body='username='+encodeURIComponent(document.getElementById('user').value)+
 '&password='+encodeURIComponent(document.getElementById('pass').value)+
 '&guard='+encodeURIComponent(document.getElementById('guard').value);
 const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body});
 const t=await r.text(); if(t)alert(t); poll();}
setInterval(poll,3000);setInterval(loadLog,false,5000);poll();loadLog();loadCfg();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _authed(self):
        if not PANEL_PASSWORD:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
        except Exception:
            return False
        return pw == PANEL_PASSWORD

    def _send(self, code, body, ctype="text/plain", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _deny(self):
        self._send(401, "unauthorized", extra={"WWW-Authenticate": 'Basic realm="swserver"'})

    def do_GET(self):
        if not self._authed():
            return self._deny()
        path = self.path.split("?")[0]
        if path == "/":
            page = PAGE.replace("USERNAME_PLACEHOLDER",
                               html.escape(os.environ.get("STEAM_USERNAME", "")))
            self._send(200, page, "text/html")
        elif path == "/api/status":
            self._send(200, json.dumps(SUP.status()), "application/json")
        elif path == "/api/log":
            self._send(200, tail_log())
        elif path == "/api/config":
            self._send(200, SUP.get_config(), "application/xml")
        elif path == "/api/auth/qr.png":
            if os.path.exists(QR_PATH):
                with open(QR_PATH, "rb") as f:
                    self._send(200, f.read(), "image/png")
            else:
                self._send(404, "no qr yet")
        else:
            self._send(404, "not found")

    def _read_form(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_POST(self):
        if not self._authed():
            return self._deny()
        path = self.path.split("?")[0]
        if path == "/api/auth/login":
            form = self._read_form()
            if not form.get("username"):
                return self._send(400, "username required")
            err = SUP.start_login(form["username"], form.get("password", ""), form.get("guard", ""))
            return self._send(200, err or "", "text/plain")
        if path == "/api/server/start":
            err = SUP.start_server()
            return self._send(200, err or "")
        if path == "/api/server/stop":
            SUP.stop_server()
            return self._send(200, "")
        if path == "/api/config/save":
            form = self._read_form()
            purge = form.get("purge", "").lower() in ("1", "true", "on")
            err = SUP.save_config(form.get("content", ""), purge=purge)
            return self._send(200, err or "")
        if path == "/api/server/reset-world":
            err = SUP.reset_world()
            return self._send(200, err or "")
        if path == "/api/server/restart":
            SUP.restart_server()
            return self._send(200, "")
        if path == "/api/update":
            if SUP.auth_state != "authenticated":
                return self._send(200, "log in to Steam first (downloads require an authenticated account)")
            if SUP.busy:
                return self._send(200, "busy: " + SUP.busy)
            SUP.update_now()
            return self._send(200, "")
        self._send(404, "not found")


def main():
    log("panel starting (port %d)" % PANEL_PORT)
    server = ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), Handler)

    def boot():
        try:
            if SUP.try_cached_login():
                if not SUP.installed():
                    threading.Thread(target=SUP.ensure_install, daemon=True).start()
                elif not SUP.server and os.environ.get("AUTO_START", "true").lower() != "false":
                    SUP.start_server()
            else:
                SUP.auth_state = "none"
                SUP.auth_message = "Not logged in - open the panel, enter Steam credentials (leave guard code blank for QR login)."
        except Exception as e:
            log("boot sequence failed: %r" % e)
    threading.Thread(target=boot, daemon=True).start()

    def shutdown(sig, frm):
        try:
            SUP.stop_server()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
