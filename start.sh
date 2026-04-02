#!/bin/bash
# Sherlock startup script — run at boot or manually
# Mounts NAS, starts Docker containers, starts Sherlock web server

set -e
LOG=~/Sherlock/logs/startup.log
echo "Wed Apr  1 21:41:56 CDT 2026: Sherlock startup beginning" >> $LOG

# 1. Wait for network
for i in {1..30}; do
  ping -c1 -W1 192.168.2.221 >/dev/null 2>&1 && break
  sleep 2
done

# 2. Mount NAS if not already mounted
if ! mount | grep -q '/Users/nijemtech/NAS'; then
  mkdir -p ~/NAS
  mount_smbfs '//admin:1qaz%40WSX3edc@192.168.2.221/Firm%20Data' ~/NAS 2>>$LOG &&     echo "Wed Apr  1 21:41:56 CDT 2026: NAS mounted" >> $LOG ||     echo "Wed Apr  1 21:41:56 CDT 2026: NAS mount FAILED" >> $LOG
else
  echo "Wed Apr  1 21:41:56 CDT 2026: NAS already mounted" >> $LOG
fi

# 3. Start Docker (ChromaDB + SearXNG)
if ! /usr/local/bin/docker info >/dev/null 2>&1; then
  open -a Docker
  for i in {1..60}; do
    /usr/local/bin/docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
cd ~/Sherlock
/usr/local/bin/docker compose up -d 2>>$LOG
echo "Wed Apr  1 21:41:56 CDT 2026: Docker containers started" >> $LOG

# 4. Wait for ChromaDB to be healthy
for i in {1..30}; do
  curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1 && break
  sleep 2
done
echo "Wed Apr  1 21:41:56 CDT 2026: ChromaDB healthy" >> $LOG

# 5. Start Ollama if not running
if ! pgrep -x ollama >/dev/null; then
  /usr/local/bin/ollama serve >/dev/null 2>&1 &
  sleep 3
fi
echo "Wed Apr  1 21:41:56 CDT 2026: Ollama running" >> $LOG

# 6. Start Sherlock web server
pkill -f 'uvicorn main:app' 2>/dev/null || true
sleep 1
cd ~/Sherlock/web
nohup ~/Sherlock/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 3000 >> ~/Sherlock/logs/sherlock-web.log 2>&1 &
echo "Wed Apr  1 21:41:56 CDT 2026: Sherlock web server started (PID $!)" >> $LOG

# 7. Verify
sleep 5
if curl -sf http://localhost:3000/ >/dev/null 2>&1; then
  echo "Wed Apr  1 21:41:56 CDT 2026: Sherlock startup COMPLETE" >> $LOG
else
  echo "Wed Apr  1 21:41:56 CDT 2026: Sherlock startup FAILED — web not responding" >> $LOG
fi
