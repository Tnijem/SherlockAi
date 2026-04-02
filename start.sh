#!/bin/bash
# Sherlock prerequisites — NAS mount, Docker, Ollama
# Called by com.sherlock.startup at boot BEFORE the web server starts
# The web server itself is managed by com.sherlock.web (KeepAlive)

set -e
LOG=~/Sherlock/logs/startup.log
mkdir -p ~/Sherlock/logs
TS=$(date '+%Y-%m-%d %H:%M:%S')

echo "${TS}: Sherlock prerequisites starting" >> "$LOG"

# 1. Wait for network (NAS at 192.168.2.221)
for i in {1..30}; do
  ping -c1 -W1 192.168.2.221 >/dev/null 2>&1 && break
  sleep 2
done

# 2. Mount NAS if not already mounted
if ! mount | grep -q '/Users/nijemtech/NAS'; then
  mkdir -p ~/NAS
  if mount_smbfs '//admin:1qaz%40WSX3edc@192.168.2.221/Firm%20Data' ~/NAS 2>>"$LOG"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S'): NAS mounted" >> "$LOG"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S'): NAS mount FAILED (will retry on next check)" >> "$LOG"
  fi
else
  echo "${TS}: NAS already mounted" >> "$LOG"
fi

# 3. Start Docker Desktop if not running
if ! /usr/local/bin/docker info >/dev/null 2>&1; then
  open -a Docker
  for i in {1..60}; do
    /usr/local/bin/docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi

# 4. Start Docker containers (ChromaDB + SearXNG)
cd ~/Sherlock
/usr/local/bin/docker compose up -d 2>>"$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S'): Docker containers started" >> "$LOG"

# 5. Wait for ChromaDB to be healthy
for i in {1..30}; do
  curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1 && break
  sleep 2
done
echo "$(date '+%Y-%m-%d %H:%M:%S'): ChromaDB healthy" >> "$LOG"

# 6. Start Ollama if not running
if ! pgrep -x ollama >/dev/null; then
  /opt/homebrew/bin/ollama serve >/dev/null 2>&1 &
  sleep 3
fi
echo "$(date '+%Y-%m-%d %H:%M:%S'): Ollama running" >> "$LOG"

echo "$(date '+%Y-%m-%d %H:%M:%S'): Prerequisites DONE — web server managed by launchd" >> "$LOG"
