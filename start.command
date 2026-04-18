#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

APP_URL="http://127.0.0.1:5001/works"

if lsof -nP -iTCP:5001 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 5001 is already in use. Opening $APP_URL"
  open "$APP_URL"
  exit 0
fi

if [ ! -x ".venv/bin/python3" ]; then
  echo ".venv が見つからないため作成します..."
  python3 -m venv .venv
  .venv/bin/python3 -m pip install -r requirements.txt
fi

echo "Starting app on http://127.0.0.1:5001"
open "$APP_URL"
exec .venv/bin/python3 app.py
