#!/bin/bash
# Sherlock uninstall — removes services and optionally data.
#
# Usage:
#   ./uninstall.sh            Interactive — prompts before each step
#   ./uninstall.sh --full     Remove EVERYTHING including DB, uploads, outputs
#   ./uninstall.sh --services Stop and remove services only (preserve data)

set -euo pipefail

SHERLOCK="${HOME}/Sherlock"
PLIST_WEB="${HOME}/Library/LaunchAgents/com.sherlock.web.plist"
PLIST_IDX="${HOME}/Library/LaunchAgents/com.sherlock.indexer.plist"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }

confirm() {
  [[ "${MODE}" != "interactive" ]] && return 0
  read -p "  $* [y/N]: " ans
  [[ "${ans}" =~ ^[Yy]$ ]]
}

MODE="interactive"
[[ "${1:-}" == "--full" ]]     && MODE="full"
[[ "${1:-}" == "--services" ]] && MODE="services"

echo ""
echo -e "${BOLD}  Sherlock Uninstall${RESET}"
echo ""

if [[ "${MODE}" == "full" ]]; then
  echo -e "  ${RED}${BOLD}WARNING: --full permanently deletes the database, uploaded files,${RESET}"
  echo -e "  ${RED}${BOLD}and saved outputs. This cannot be undone.${RESET}"
  echo ""
  read -p "  Type CONFIRM to proceed: " ans
  [[ "${ans}" == "CONFIRM" ]] || { echo "  Aborted."; exit 0; }
fi

# ── launchd agents ────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}launchd agents${RESET}"

if confirm "Unload and remove sherlock-web agent?"; then
  launchctl unload "${PLIST_WEB}" 2>/dev/null && ok "sherlock-web unloaded." || warn "sherlock-web was not loaded."
  rm -f "${PLIST_WEB}" && ok "Plist removed." || true
fi

if confirm "Unload and remove sherlock-indexer agent?"; then
  launchctl unload "${PLIST_IDX}" 2>/dev/null && ok "sherlock-indexer unloaded." || warn "sherlock-indexer was not loaded."
  rm -f "${PLIST_IDX}" && ok "Plist removed." || true
fi

# ── Docker services ───────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Docker services${RESET}"

if confirm "Stop and remove sherlock Docker containers?"; then
  cd "${SHERLOCK}"
  docker compose down && ok "Containers stopped and removed." || warn "docker compose down failed (may already be stopped)."
fi

if [[ "${MODE}" == "full" ]]; then
  if confirm "Remove Docker volumes (Ollama models + ChromaDB vectors)?"; then
    cd "${SHERLOCK}"
    docker compose down -v 2>/dev/null || true
    docker volume rm sherlock_ollama_data sherlock_chroma_data 2>/dev/null && ok "Docker volumes removed." || warn "Volumes may already be removed."
  fi
fi

# ── Data (full mode only) ─────────────────────────────────────────────────────
if [[ "${MODE}" == "full" ]]; then
  echo ""
  echo -e "  ${BOLD}Data files${RESET}"

  if confirm "Delete Sherlock database (users, conversations, index state)?"; then
    rm -f "${SHERLOCK}/data/sherlock.db" && ok "sherlock.db deleted." || true
  fi

  if confirm "Delete all user-uploaded files (${SHERLOCK}/uploads/)?"; then
    rm -rf "${SHERLOCK}/uploads/" && ok "Uploads deleted." || true
  fi

  OUTPUTS_DIR=$(grep "^OUTPUTS_DIR=" "${SHERLOCK}/sherlock.conf" 2>/dev/null | cut -d= -f2- || echo "${SHERLOCK}/outputs")
  echo -e "  ${YELLOW}Outputs directory: ${OUTPUTS_DIR}${RESET}"
  if confirm "Delete saved outputs? (PERMANENT — contains attorney work product)"; then
    rm -rf "${OUTPUTS_DIR}" && ok "Outputs deleted." || warn "Could not delete ${OUTPUTS_DIR} — may be on NAS."
  fi

  if confirm "Delete downloaded Whisper models (${SHERLOCK}/models/)?"; then
    rm -rf "${SHERLOCK}/models/" && ok "Models deleted." || true
  fi

  if confirm "Delete local ChromaDB data (${SHERLOCK}/chromadb/)?"; then
    rm -rf "${SHERLOCK}/chromadb/" && ok "ChromaDB local data deleted." || true
  fi

  if confirm "Delete sherlock.conf?"; then
    rm -f "${SHERLOCK}/sherlock.conf" && ok "sherlock.conf deleted." || true
  fi

  if confirm "Delete log files?"; then
    rm -f "${SHERLOCK}/logs/"*.log && ok "Logs deleted." || true
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}Uninstall complete.${RESET}"
echo ""

if [[ "${MODE}" != "full" ]]; then
  echo "  Data preserved. To also remove all data:"
  echo "    ./uninstall.sh --full"
  echo ""
fi

if [[ "${MODE}" == "services" ]]; then
  echo "  To restart services:"
  echo "    ./setup.sh --update"
  echo ""
fi
