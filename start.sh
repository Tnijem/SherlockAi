#!/bin/bash
# Sherlock startup — prerequisites + web server
set -e
LOG=~/Sherlock/logs/startup.log
mkdir -p ~/Sherlock/logs
TS() { date "+%Y-%m-%d %H:%M:%S"; }

echo "$(TS): Sherlock startup beginning" >> "$LOG"

# 1. Wait for network
for i in {1..30}; do
  ping -c1 -W1 192.168.2.221 >/dev/null 2>&1 && break
  sleep 2
done

# 2. Mount NAS
if ! mount | grep -q "/Users/nijemtech/NAS"; then
  mkdir -p ~/NAS
  mount_smbfs "//admin:1qaz%40WSX3edc@192.168.2.221/Firm%20Data" ~/NAS 2>>"$LOG" \
    && echo "$(TS): NAS mounted" >> "$LOG" \
    || echo "$(TS): NAS mount FAILED" >> "$LOG"
fi

# 3. Docker
if ! /usr/local/bin/docker info >/dev/null 2>&1; then
  open -a Docker
  for i in {1..60}; do
    /usr/local/bin/docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
cd ~/Sherlock && /usr/local/bin/docker compose up -d 2>>"$LOG"

# 4. Wait for ChromaDB
for i in {1..30}; do
  curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1 && break
  sleep 2
done

# 5. Ollama
if ! pgrep -x ollama >/dev/null; then
  /opt/homebrew/bin/ollama serve >/dev/null 2>&1 &
  sleep 3
fi

# 6. Start web server (kill any stale)
pkill -f "uvicorn main:app" 2>/dev/null || true
sleep 1
cd ~/Sherlock/web
nohup ~/Sherlock/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 3000 >> ~/Sherlock/logs/sherlock-web.log 2>&1 &
echo "$(TS): Sherlock started (PID $!)" >> "$LOG"

# 7. Verify
sleep 5
if curl -sf http://localhost:3000/ >/dev/null 2>&1; then
  echo "$(TS): Sherlock startup COMPLETE" >> "$LOG"
else
  echo "$(TS): Sherlock startup FAILED" >> "$LOG"
fi
