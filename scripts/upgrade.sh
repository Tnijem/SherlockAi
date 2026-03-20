#!/bin/bash
# upgrade.sh — Pull latest GitHub release and apply in-place
#
# Usage:
#   ./upgrade.sh                    # interactive, prompts for confirmation
#   ./upgrade.sh --yes              # non-interactive (scheduled/silent run)
#   ./upgrade.sh --version v1.2.3   # pin to specific release tag

set -euo pipefail

SHERLOCK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITHUB_REPO="Tnijem/SherlockAi"
LOG_FILE="${SHERLOCK_DIR}/logs/upgrade-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "${SHERLOCK_DIR}/logs"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET}  $*" | tee -a "${LOG_FILE}"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*" | tee -a "${LOG_FILE}"; }
err()  { echo -e "  ${RED}✗${RESET}  $*" | tee -a "${LOG_FILE}"; exit 1; }
hdr()  { echo -e "\n${BOLD}${BLUE}$*${RESET}" | tee -a "${LOG_FILE}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*" | tee -a "${LOG_FILE}"; }

ASSUME_YES=false
PINNED_VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)      ASSUME_YES=true; shift ;;
    --version)  PINNED_VERSION="$2"; shift 2 ;;
    *)          shift ;;
  esac
done

echo -e "\n${BOLD}Sherlock Upgrade${RESET}" | tee -a "${LOG_FILE}"

# ── Current version ───────────────────────────────────────────────────────────
CURRENT_VERSION="unknown"
[[ -f "${SHERLOCK_DIR}/VERSION" ]] && CURRENT_VERSION=$(cat "${SHERLOCK_DIR}/VERSION" | tr -d '[:space:]')
echo "  Current : ${CURRENT_VERSION}" | tee -a "${LOG_FILE}"

# ── Fetch latest release from GitHub ─────────────────────────────────────────
hdr "Checking for updates"

if [[ -n "${PINNED_VERSION}" ]]; then
  API_URL="https://api.github.com/repos/${GITHUB_REPO}/releases/tags/${PINNED_VERSION}"
else
  API_URL="https://api.github.com/repos/${GITHUB_REPO}/releases/latest"
fi

step "Querying GitHub releases API..."
RELEASE_JSON=$(curl -sf "${API_URL}" 2>/dev/null) || err "Cannot reach GitHub. Check network or try again."

LATEST_TAG=$(echo "${RELEASE_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null) \
  || err "Could not parse release tag from GitHub response."

ASSET_URL=$(echo "${RELEASE_JSON}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assets = data.get('assets', [])
for a in assets:
    if a['name'] == 'sherlock-source.tar.gz':
        print(a['browser_download_url'])
        break
" 2>/dev/null) || true

if [[ -z "${ASSET_URL}" ]]; then
  # Fallback: try tarball_url (source archive)
  ASSET_URL=$(echo "${RELEASE_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tarball_url',''))" 2>/dev/null) || true
fi

[[ -z "${ASSET_URL}" ]] && err "No downloadable asset found in release ${LATEST_TAG}."

echo "  Latest  : ${LATEST_TAG}" | tee -a "${LOG_FILE}"
echo "  Asset   : ${ASSET_URL}" | tee -a "${LOG_FILE}"

if [[ "${LATEST_TAG}" == "${CURRENT_VERSION}" ]] && [[ "${ASSUME_YES}" == "false" ]]; then
  echo -e "\n  ${GREEN}Already up to date.${RESET}"
  exit 0
fi

# ── Confirm ───────────────────────────────────────────────────────────────────
if [[ "${ASSUME_YES}" == "false" ]]; then
  echo ""
  read -r -p "  Upgrade ${CURRENT_VERSION} → ${LATEST_TAG}? [y/N] " CONFIRM
  [[ "${CONFIRM}" =~ ^[Yy]$ ]] || { echo "  Cancelled."; exit 0; }
fi

# ── Download ──────────────────────────────────────────────────────────────────
hdr "Downloading ${LATEST_TAG}"
TMP_DIR=$(mktemp -d)
ARCHIVE="${TMP_DIR}/sherlock-source.tar.gz"

step "Downloading from GitHub..."
curl -sfL "${ASSET_URL}" -o "${ARCHIVE}" 2>&1 | tee -a "${LOG_FILE}" \
  || err "Download failed."

ok "Downloaded ($(du -sh "${ARCHIVE}" | awk '{print $1}'))"

# ── Extract ───────────────────────────────────────────────────────────────────
hdr "Extracting"
EXTRACT_DIR="${TMP_DIR}/source"
mkdir -p "${EXTRACT_DIR}"
tar -xzf "${ARCHIVE}" -C "${EXTRACT_DIR}" --strip-components=1 2>&1 | tee -a "${LOG_FILE}" \
  || err "Extraction failed."
ok "Extracted"

# ── Apply ─────────────────────────────────────────────────────────────────────
hdr "Applying update"
step "Syncing files (preserving data, uploads, logs)..."

rsync -a --delete \
  --exclude='data/' \
  --exclude='uploads/' \
  --exclude='outputs/' \
  --exclude='logs/' \
  --exclude='index_cache/' \
  --exclude='chroma/' \
  --exclude='chromadb/' \
  --exclude='models/' \
  --exclude='ollama/' \
  --exclude='venv/' \
  --exclude='sherlock.conf' \
  --exclude='.env' \
  "${EXTRACT_DIR}/" "${SHERLOCK_DIR}/" \
  2>&1 | tee -a "${LOG_FILE}" || err "rsync failed."

ok "Files synced"

# ── Python deps (if requirements changed) ────────────────────────────────────
if [[ -f "${SHERLOCK_DIR}/web/requirements.txt" ]]; then
  step "Updating Python dependencies..."
  "${SHERLOCK_DIR}/venv/bin/pip" install -q -r "${SHERLOCK_DIR}/web/requirements.txt" \
    2>&1 | tail -3 | tee -a "${LOG_FILE}" || warn "pip install had warnings (non-fatal)"
  ok "Dependencies updated"
fi

# ── Write new version ─────────────────────────────────────────────────────────
echo "${LATEST_TAG}" > "${SHERLOCK_DIR}/VERSION"
ok "Version file updated → ${LATEST_TAG}"

# ── Restart Sherlock ──────────────────────────────────────────────────────────
hdr "Restarting Sherlock"

if systemctl is-active --quiet sherlock 2>/dev/null; then
  step "Restarting via systemd..."
  systemctl restart sherlock
  sleep 3
  systemctl is-active --quiet sherlock && ok "Sherlock restarted via systemd" || err "systemd restart failed"
else
  step "Sending SIGHUP to uvicorn for graceful reload..."
  pkill -HUP -f "uvicorn main:app" 2>/dev/null && ok "Reload signal sent" \
    || warn "Could not signal uvicorn — you may need to restart manually"
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -rf "${TMP_DIR}"

echo ""
echo -e "${BOLD}${GREEN}Upgrade complete: ${CURRENT_VERSION} → ${LATEST_TAG}${RESET}"
echo "  Log: ${LOG_FILE}"
