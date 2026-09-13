#!/bin/bash
# Thin bootstrap - all logic lives in the panel (python).
set -u
export HOME="${HOME:-/home/steam}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
mkdir -p "$HOME/Steam/logs" "$HOME/.wine"
exec python3 /opt/panel.py
