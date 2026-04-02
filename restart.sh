#!/bin/bash
# Sherlock — quick restart (web server only)
set -euo pipefail
GREEN="\033[0;32m"; BOLD="\033[1m"; RESET="\033[0m"
ok() { echo -e "  ${GREEN}✓${RESET}  $*"; }

echo -e "\n${BOLD}  Sherlock Restart${RESET}\n"

pkill -f "uvicorn main:app" 2>/dev/null && ok "Old server killed" || ok "No server running"
sleep 2

cd ~/Sherlock/web
nohup ~/Sherlock/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 3000 >> ~/Sherlock/logs/sherlock-web.log 2>&1 &
ok "Server started (PID $!)"

sleep 5
if curl -sf http://localhost:3000/ >/dev/null; then
  ok "Sherlock is UP"
  HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "localhost")
  echo -e "\n  Web UI: http://${HOST_IP}:3000\n"
else
  echo -e "  \033[0;31m✗\033[0m  Server not responding"
fi
