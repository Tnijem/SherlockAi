#!/bin/bash
# Sherlock — full restart script.
# Bounces launchd agents + Docker services in the correct order.
# Safe to run at any time; services don't need to be running first.

set -euo pipefail

SHERLOCK="${HOME}/Sherlock"
PLIST_WEB="${HOME}/Library/LaunchAgents/com.sherlock.web.plist"
PLIST_IDX="${HOME}/Library/LaunchAgents/com.sherlock.indexer.plist"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
info() { echo -e "  ${YELLOW}→${RESET}  $*"; }

echo ""
echo -e "${BOLD}  Sherlock Restart${RESET}"
echo ""

# ── 1. Stop launchd agents ────────────────────────────────────────────────────
echo -e "  ${BOLD}Stopping launchd agents…${RESET}"

if launchctl list | grep -q "com.sherlock.web"; then
  launchctl unload "${PLIST_WEB}" 2>/dev/null && ok "sherlock-web stopped." || true
else
  info "sherlock-web was not running."
fi

if launchctl list | grep -q "com.sherlock.indexer"; then
  launchctl unload "${PLIST_IDX}" 2>/dev/null && ok "sherlock-indexer stopped." || true
else
  info "sherlock-indexer was not running."
fi

# ── 2. Bounce Docker services ─────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Restarting Docker services (ChromaDB + SearXNG)…${RESET}"

cd "${SHERLOCK}"
docker compose down --remove-orphans 2>/dev/null && ok "Containers stopped."
docker compose up -d && ok "Containers started."

# ── 3. Wait for services to be healthy ───────────────────────────────────────
echo ""
echo -e "  ${BOLD}Waiting for services to be ready…${RESET}"

OLLAMA_URL=$(grep "^OLLAMA_URL=" "${SHERLOCK}/sherlock.conf" 2>/dev/null | cut -d= -f2- || echo "http://localhost:11434")
CHROMA_URL=$(grep "^CHROMA_URL=" "${SHERLOCK}/sherlock.conf" 2>/dev/null | cut -d= -f2- || echo "http://localhost:8000")

_wait_http() {
  local url="$1" label="$2" tries=0
  while ! curl -sf "${url}" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [[ ${tries} -ge 30 ]]; then
      echo -e "  \033[0;31m✗\033[0m  ${label} did not become ready in time." >&2
      return 1
    fi
    sleep 2
  done
  ok "${label} is ready."
}

_wait_http "${OLLAMA_URL}/api/tags"          "Ollama"
_wait_http "${CHROMA_URL}/api/v2/heartbeat"  "ChromaDB"

# ── 4. Restart launchd agents ─────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Starting launchd agents…${RESET}"

if [[ -f "${PLIST_WEB}" ]]; then
  launchctl load "${PLIST_WEB}" && ok "sherlock-web started."
else
  echo -e "  \033[0;31m✗\033[0m  ${PLIST_WEB} not found — run setup.sh first."
fi

if [[ -f "${PLIST_IDX}" ]]; then
  launchctl load "${PLIST_IDX}" && ok "sherlock-indexer started."
else
  info "sherlock-indexer plist not found — skipping."
fi

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
ok "Sherlock is back up."

PORT=$(grep "^PORT=" "${SHERLOCK}/sherlock.conf" 2>/dev/null | cut -d= -f2- || echo "3000")
HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "localhost")
echo ""
echo -e "  Web UI:  http://${HOST_IP}:${PORT}"
echo -e "  Local:   http://localhost:${PORT}"
echo ""
