#!/bin/bash
# Sherlock — full restart script.
# Bounces Docker services + launchd agents in the correct order.

set -euo pipefail

SHERLOCK="${HOME}/Sherlock"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
info() { echo -e "  ${YELLOW}→${RESET}  $*"; }
fail() { echo -e "  ${RED}✗${RESET}  $*"; }

echo ""
echo -e "${BOLD}  Sherlock Restart${RESET}"
echo ""

# ── 1. Stop web server ──────────────────────────────────────────────────────
echo -e "  ${BOLD}Stopping web server…${RESET}"
if launchctl list | grep -q "com.sherlock.web"; then
  launchctl unload ~/Library/LaunchAgents/com.sherlock.web.plist 2>/dev/null
  ok "sherlock-web stopped"
else
  info "sherlock-web was not running"
fi
# Kill any stray uvicorn processes
pkill -f 'uvicorn main:app' 2>/dev/null && ok "killed stray uvicorn" || true
sleep 1

# ── 2. Bounce Docker services ───────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Restarting Docker services…${RESET}"
cd "${SHERLOCK}"
docker compose down --remove-orphans 2>/dev/null && ok "Containers stopped"
docker compose up -d && ok "Containers started"

# ── 3. Wait for services ────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Waiting for services…${RESET}"

_wait_http() {
  local url="$1" label="$2" tries=0
  while ! curl -sf "${url}" >/dev/null 2>&1; do
    tries=$((tries + 1))
    [[ ${tries} -ge 30 ]] && { fail "${label} not ready"; return 1; }
    sleep 2
  done
  ok "${label} ready"
}

_wait_http "http://localhost:8000/api/v2/heartbeat" "ChromaDB"

# Check Ollama
if pgrep -x ollama >/dev/null; then
  ok "Ollama running"
else
  /opt/homebrew/bin/ollama serve >/dev/null 2>&1 &
  sleep 3
  ok "Ollama started"
fi

# ── 4. Check NAS mount ──────────────────────────────────────────────────────
if mount | grep -q '/Users/nijemtech/NAS'; then
  ok "NAS mounted"
else
  info "NAS not mounted — attempting mount"
  mkdir -p ~/NAS
  mount_smbfs '//admin:1qaz%40WSX3edc@192.168.2.221/Firm%20Data' ~/NAS 2>/dev/null && ok "NAS mounted" || fail "NAS mount failed"
fi

# ── 5. Start web server via launchd ─────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Starting web server…${RESET}"
launchctl load ~/Library/LaunchAgents/com.sherlock.web.plist && ok "sherlock-web started (KeepAlive)"

# ── 6. Verify ───────────────────────────────────────────────────────────────
sleep 4
if curl -sf http://localhost:3000/ >/dev/null; then
  ok "Sherlock is UP"
else
  fail "Web server not responding on :3000"
fi

HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "localhost")
echo ""
echo -e "  Web UI:  http://${HOST_IP}:3000"
echo ""
