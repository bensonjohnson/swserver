#!/bin/bash
# Thin bootstrap - all logic lives in the panel (python).
set -u
export HOME="${HOME:-/home/steam}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
mkdir -p "$HOME/Steam/logs" "$HOME/.wine"
# re-seed steamcmd if an empty volume shadows the image copy (k8s PVCs)
if [ ! -x "$HOME/steamcmd/steamcmd.sh" ] && [ -d /opt/steamcmd-fallback ]; then
  echo "seeding $HOME/steamcmd from /opt/steamcmd-fallback"
  mkdir -p "$HOME/steamcmd"
  cp -a /opt/steamcmd-fallback/. "$HOME/steamcmd/"
fi
exec python3 /opt/panel.py
