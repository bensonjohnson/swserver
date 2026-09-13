# Stormworks Server in Docker

Container registry: `gitea.gokickrocks.org/bensonjohnson/sw-server` \
https://gitea.gokickrocks.org/bensonjohnson/sw-server \
(mirror: https://github.com/bensonjohnson/swserver)

A Stormworks: Build and Rescue dedicated server (Windows `server64.exe` under
Wine) with a built-in **web control panel** for Steam login (including QR
login via the Steam mobile app), server start/stop/restart, updates, and logs.

## Quick start (Docker Compose)

```bash
cp .env.example .env   # set STEAM_USERNAME and STEAM_GSLT
docker compose up -d --build
open http://localhost:8080
```

1. Open the panel → enter your Steam username + password, **leave the guard
   code blank** → a QR code appears → scan it with the Steam mobile app
   (Steam → QR code tab) and confirm.
2. The server files download automatically and the server starts.
3. The login token is persisted on the `swserver-steam` volume, so **you only
   need to do the QR login once** - restarts log in automatically with the
   cached credentials.

If your account sends a Steam Guard code to email instead of the app, type it
into the guard-code field (it expires quickly - have it ready).

## Panel

Served on port 8080 (`PANEL_PORT`):

- **Status** - server running/stopped, uptime, install state, last update
- **Start / Stop / Restart / Check for updates** buttons
- **Steam login** - cached-token, password+guard code, or QR login
- **Log** - combined SteamCMD + server output

Set `PANEL_PASSWORD` (HTTP basic auth) before exposing the panel anywhere
other than localhost/LAN.

## How authentication works

- `STEAM_USERNAME` is used for cached logins; the password is never taken
  from the environment (enter it in the panel - it goes only to the local
  SteamCMD session, never to `ps` or logs).
- After one successful interactive login SteamCMD's machine token is stored
  on the `swserver-steam` volume; every later start uses
  `steamcmd +login <user>` non-interactively.
- The SteamCMD client itself is persisted on `swserver-steamcmd` (the base
  image ships a current, pre-updated client, avoiding the flaky
  self-bootstrap of the old public tarball).

## Kubernetes

```bash
kubectl apply -f k8s/persistent-storage.yaml
kubectl -n game-servers create secret docker-registry gitea-registry \
  --docker-server=gitea.gokickrocks.org \
  --docker-username=<gitea-user> --docker-password=<gitea-token-with-package-write>
kubectl -n game-servers create secret generic steam-credentials \
  --from-literal=STEAM_USERNAME=<user> --from-literal=STEAM_GSLT=<token> \
  --from-literal=PANEL_PASSWORD=<panel-pass>
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/NodePort/   # or k8s/loadbalancer/
```

Reach the NodePort panel at `http://<node>:32580`, log in once via QR, done.

## Environment variables

| Variable | Purpose |
|---|---|
| `STEAM_USERNAME` | account that owns Stormworks (cached logins) |
| `STEAM_GSLT` | Game Server Login Token → written to `server.cfg` |
| `PANEL_PORT` | control panel port (default 8080) |
| `PANEL_PASSWORD` | basic-auth password for the panel |
| `AUTO_START` | start server automatically at boot (default true) |

## Notes

- Game ports: 25564-25566 (TCP+UDP).
- The dedicated server is Windows-only and runs under Wine + Xvfb; saves
  live in the Wine prefix volume (`/home/steam/.wine`).
- Requires an account that owns Stormworks - the server depots are not
  anonymous downloads.
