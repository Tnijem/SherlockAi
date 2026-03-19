#!/bin/bash
# bundle.sh — Create a portable, air-gapped Sherlock install bundle
#
# Packages everything needed to deploy Sherlock on a machine with no internet:
#   - Docker images (Ollama + ChromaDB)
#   - Ollama models (sherlock-rag, mxbai-embed-large)
#   - Python wheelhouse (all pip dependencies)
#   - Whisper model files
#   - Nginx Homebrew bottle
#   - Sherlock source code
#   - A self-contained restore script
#
# Output: ~/sherlock-bundle-YYYYMMDD.tar.gz  (~10-15GB depending on models)
#
# Usage:
#   ~/Sherlock/bundle.sh
#   ~/Sherlock/bundle.sh --output /Volumes/USB/sherlock-bundle.tar.gz

set -euo pipefail

SHERLOCK="${HOME}/Sherlock"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
DEFAULT_OUT="${HOME}/sherlock-bundle-${TIMESTAMP}.tar.gz"
OUTPUT="${1:-}"
[[ "${OUTPUT}" == "--output" ]] && OUTPUT="${2:-${DEFAULT_OUT}}" || OUTPUT="${OUTPUT:-${DEFAULT_OUT}}"

BUNDLE_DIR="$(mktemp -d)/sherlock-bundle"
LOG="${SHERLOCK}/logs/bundle-${TIMESTAMP}.log"
mkdir -p "${BUNDLE_DIR}" "${SHERLOCK}/logs"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
ok()    { echo -e "  ${GREEN}✓${RESET}  $*" | tee -a "${LOG}"; }
warn()  { echo -e "  ${YELLOW}⚠${RESET}  $*" | tee -a "${LOG}"; }
err()   { echo -e "  ${RED}✗${RESET}  $*" | tee -a "${LOG}"; exit 1; }
hdr()   { echo -e "\n${BOLD}${BLUE}$*${RESET}" | tee -a "${LOG}"; }
step()  { echo -e "  ${BLUE}→${RESET}  $*" | tee -a "${LOG}"; }
size()  { du -sh "$1" 2>/dev/null | awk '{print $1}'; }

echo -e "\n${BOLD}Sherlock Air-Gap Bundle Creator${RESET}"
echo "  Output : ${OUTPUT}"
echo "  Staging: ${BUNDLE_DIR}"
echo ""

# ── Preflight ─────────────────────────────────────────────────────────────────
hdr "Preflight checks"

command -v docker &>/dev/null  || err "Docker not found"
command -v python3 &>/dev/null || err "python3 not found"
docker info &>/dev/null        || err "Docker daemon not running"

# Check Sherlock containers are running (models need to be pulled)
if ! docker ps --format '{{.Names}}' | grep -q "sherlock-ollama"; then
  warn "sherlock-ollama container not running — starting Docker services"
  cd "${SHERLOCK}" && docker compose up -d
  sleep 10
fi

ok "Preflight passed"

# ── 1. Docker images ──────────────────────────────────────────────────────────
hdr "1. Saving Docker images"
step "This takes a few minutes for large images..."

docker save \
  ollama/ollama:latest \
  chromadb/chroma:latest \
  | gzip > "${BUNDLE_DIR}/docker-images.tar.gz"

ok "Docker images saved ($(size "${BUNDLE_DIR}/docker-images.tar.gz"))"

# ── 2. Ollama models ──────────────────────────────────────────────────────────
hdr "2. Exporting Ollama models"

MODELS_DIR="${BUNDLE_DIR}/ollama-models"
mkdir -p "${MODELS_DIR}"

# Copy models from the Docker volume via a temporary container
step "Copying Ollama model files from Docker volume..."

docker run --rm \
  -v sherlock_ollama_data:/ollama_data:ro \
  -v "${MODELS_DIR}:/export" \
  alpine:latest \
  sh -c "cp -r /ollama_data/. /export/ 2>/dev/null || cp -r /ollama_data /export/"  \
  2>/dev/null \
|| {
  # Fallback: try from running container
  warn "Volume copy failed — trying running container fallback"
  docker exec sherlock-ollama sh -c "tar -czf - /root/.ollama 2>/dev/null" \
    > "${MODELS_DIR}/ollama-home.tar.gz" \
    && ok "Models exported via container" \
    || warn "Could not export models — they will be pulled on first run"
}

ok "Ollama models saved ($(size "${MODELS_DIR}"))"

# ── 3. Python wheelhouse ──────────────────────────────────────────────────────
hdr "3. Downloading Python wheels"

WHEELS_DIR="${BUNDLE_DIR}/wheelhouse"
mkdir -p "${WHEELS_DIR}"

step "Downloading all wheels for $(python3 --version)..."
python3 -m pip download \
  -r "${SHERLOCK}/web/requirements.txt" \
  -d "${WHEELS_DIR}" \
  --quiet \
  2>&1 | tail -5 | tee -a "${LOG}" || warn "Some wheels may have failed to download"

ok "Python wheels saved ($(size "${WHEELS_DIR}"), $(ls "${WHEELS_DIR}" | wc -l | tr -d ' ') packages)"

# ── 4. Whisper models ─────────────────────────────────────────────────────────
hdr "4. Whisper model files"

WHISPER_SRC="${SHERLOCK}/models/whisper"
WHISPER_DEST="${BUNDLE_DIR}/whisper-models"

if [[ -d "${WHISPER_SRC}" ]] && [[ -n "$(ls -A "${WHISPER_SRC}" 2>/dev/null)" ]]; then
  cp -r "${WHISPER_SRC}" "${WHISPER_DEST}"
  ok "Whisper models copied ($(size "${WHISPER_DEST}"))"
else
  warn "No Whisper models found at ${WHISPER_SRC}"
  warn "Whisper will download models on first use (requires internet on target)"
  mkdir -p "${WHISPER_DEST}"
fi

# ── 5. Nginx Homebrew bottle ──────────────────────────────────────────────────
hdr "5. Nginx Homebrew bottle"

NGINX_BOTTLE_DIR="${BUNDLE_DIR}/homebrew-bottles"
mkdir -p "${NGINX_BOTTLE_DIR}"

if command -v nginx &>/dev/null; then
  ok "Nginx already installed — bundling installed binary info"
  echo "$(nginx -v 2>&1)" > "${NGINX_BOTTLE_DIR}/nginx-version.txt"
fi

step "Fetching nginx and dependencies..."
brew fetch --deps nginx --bottle-tag=arm64_sequoia 2>/dev/null \
  | grep "Downloaded to" \
  | awk '{print $NF}' \
  | while read -r bottle; do
      cp "${bottle}" "${NGINX_BOTTLE_DIR}/" 2>/dev/null && ok "  Copied: $(basename "${bottle}")" || true
    done

# Try Intel fallback
if [[ -z "$(ls -A "${NGINX_BOTTLE_DIR}"/*.tar.gz 2>/dev/null)" ]]; then
  brew fetch --deps nginx --bottle-tag=sequoia 2>/dev/null \
    | grep "Downloaded to" \
    | awk '{print $NF}' \
    | while read -r bottle; do
        cp "${bottle}" "${NGINX_BOTTLE_DIR}/" 2>/dev/null || true
      done
fi

BOTTLE_COUNT=$(ls "${NGINX_BOTTLE_DIR}"/*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
if [[ "${BOTTLE_COUNT}" -gt 0 ]]; then
  ok "Nginx bottles saved (${BOTTLE_COUNT} bottles, $(size "${NGINX_BOTTLE_DIR}"))"
else
  warn "Could not fetch nginx bottles — target will need brew installed"
  echo "brew install nginx" > "${NGINX_BOTTLE_DIR}/README.txt"
fi

# ── 6. Sherlock source ────────────────────────────────────────────────────────
hdr "6. Sherlock source code"

SOURCE_DEST="${BUNDLE_DIR}/sherlock-source"
mkdir -p "${SOURCE_DEST}"

# Copy everything except large/generated directories
rsync -a \
  --exclude='.git' \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='data/*.db' \
  --exclude='uploads/*' \
  --exclude='outputs/*' \
  --exclude='logs/*' \
  --exclude='index_cache/*' \
  --exclude='chroma/' \
  --exclude='chromadb/' \
  --exclude='models/' \
  --exclude='ollama/' \
  "${SHERLOCK}/" "${SOURCE_DEST}/"

ok "Source copied ($(size "${SOURCE_DEST}"))"

# ── 7. Restore script ─────────────────────────────────────────────────────────
hdr "7. Generating restore script"

cat > "${BUNDLE_DIR}/restore.sh" <<'RESTORE_EOF'
#!/bin/bash
# restore.sh — Deploy Sherlock from air-gap bundle
# Run this on the target machine after extracting the bundle.
#
# Usage:
#   ./restore.sh
#   ./restore.sh --hostname sherlock.lawfirm.local --dest /opt/sherlock

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${HOME}/Sherlock"
HOSTNAME_ARG="sherlock.local"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)      DEST="$2";         shift 2 ;;
    --hostname)  HOSTNAME_ARG="$2"; shift 2 ;;
    *)           shift ;;
  esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*"; exit 1; }
hdr()  { echo -e "\n${BOLD}${BLUE}$*${RESET}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*"; }

echo -e "\n${BOLD}Sherlock Air-Gap Restore${RESET}"
echo "  Bundle : ${BUNDLE_DIR}"
echo "  Target : ${DEST}"
echo "  Host   : ${HOSTNAME_ARG}"
echo ""

# ── Preflight ─────────────────────────────────────────────────────────────────
hdr "Preflight"
command -v docker &>/dev/null || err "Docker not installed. Install Docker Desktop first."
docker info &>/dev/null       || err "Docker not running. Start Docker Desktop first."
command -v brew &>/dev/null   || err "Homebrew not installed. Install from https://brew.sh (offline: bundle includes installer)"
ok "Preflight passed"

# ── 1. Load Docker images ─────────────────────────────────────────────────────
hdr "1. Loading Docker images"
if [[ -f "${BUNDLE_DIR}/docker-images.tar.gz" ]]; then
  step "Loading (this takes a few minutes)..."
  docker load < "${BUNDLE_DIR}/docker-images.tar.gz"
  ok "Docker images loaded"
else
  err "docker-images.tar.gz not found in bundle"
fi

# ── 2. Install Sherlock source ────────────────────────────────────────────────
hdr "2. Installing Sherlock source"
if [[ -d "${DEST}" ]]; then
  warn "${DEST} already exists — merging (existing data preserved)"
fi
rsync -a --exclude='data/' --exclude='uploads/' --exclude='outputs/' \
  "${BUNDLE_DIR}/sherlock-source/" "${DEST}/"
ok "Source installed to ${DEST}"

# ── 3. Python virtualenv + wheels ────────────────────────────────────────────
hdr "3. Python environment"
VENV="${DEST}/venv"
if [[ ! -d "${VENV}" ]]; then
  python3 -m venv "${VENV}"
  ok "Virtualenv created"
else
  ok "Virtualenv exists"
fi

step "Installing from wheelhouse (no internet required)..."
"${VENV}/bin/pip" install \
  --no-index \
  --find-links "${BUNDLE_DIR}/wheelhouse" \
  -r "${DEST}/web/requirements.txt" \
  --quiet \
  && ok "Python deps installed" \
  || {
    warn "Offline install incomplete — falling back to PyPI for missing packages"
    "${VENV}/bin/pip" install \
      --find-links "${BUNDLE_DIR}/wheelhouse" \
      -r "${DEST}/web/requirements.txt" \
      --quiet
    ok "Python deps installed (hybrid)"
  }

# ── 4. Whisper models ─────────────────────────────────────────────────────────
hdr "4. Whisper models"
WHISPER_DEST="${DEST}/models/whisper"
mkdir -p "${WHISPER_DEST}"
if [[ -n "$(ls -A "${BUNDLE_DIR}/whisper-models/" 2>/dev/null)" ]]; then
  cp -rn "${BUNDLE_DIR}/whisper-models/." "${WHISPER_DEST}/"
  ok "Whisper models installed"
else
  warn "No Whisper models in bundle — will download on first audio transcription"
fi

# ── 5. Ollama models ──────────────────────────────────────────────────────────
hdr "5. Ollama models"
step "Starting Ollama container to load models..."
cd "${DEST}" && docker compose up -d ollama
sleep 15  # wait for Ollama to be ready

# Check for tarball (container-exported) vs directory (volume-copied)
if [[ -f "${BUNDLE_DIR}/ollama-models/ollama-home.tar.gz" ]]; then
  step "Importing models from archive..."
  docker exec sherlock-ollama sh -c "mkdir -p /root/.ollama"
  docker cp "${BUNDLE_DIR}/ollama-models/ollama-home.tar.gz" sherlock-ollama:/tmp/
  docker exec sherlock-ollama sh -c "cd / && tar -xzf /tmp/ollama-home.tar.gz --strip-components=1"
  ok "Models imported from archive"
elif [[ -n "$(ls -A "${BUNDLE_DIR}/ollama-models/" 2>/dev/null)" ]]; then
  step "Copying model files into volume..."
  docker exec sherlock-ollama mkdir -p /root/.ollama
  docker cp "${BUNDLE_DIR}/ollama-models/." sherlock-ollama:/root/.ollama/
  ok "Models copied into container"
else
  warn "No Ollama models in bundle"
  warn "Pulling models now (requires internet or pull from another machine)..."
  docker exec sherlock-ollama ollama pull mxbai-embed-large || warn "Failed to pull mxbai-embed-large"
  docker exec sherlock-ollama ollama pull llama3.1:8b       || warn "Failed to pull llama3.1:8b"
fi

# Start ChromaDB too
docker compose up -d chroma
ok "Docker services started"

# ── 6. Nginx ──────────────────────────────────────────────────────────────────
hdr "6. Nginx"
if command -v nginx &>/dev/null; then
  ok "Nginx already installed"
else
  step "Installing nginx from Homebrew bottles..."
  BOTTLE_DIR="${BUNDLE_DIR}/homebrew-bottles"
  if [[ -n "$(ls -A "${BOTTLE_DIR}"/*.tar.gz 2>/dev/null)" ]]; then
    for bottle in "${BOTTLE_DIR}"/*.tar.gz; do
      brew install "${bottle}" 2>/dev/null && ok "Installed: $(basename "${bottle}")" || warn "Failed: $(basename "${bottle}")"
    done
  else
    warn "No bottles in bundle — installing nginx via brew (requires internet)"
    brew install nginx
  fi
fi

bash "${DEST}/nginx/install.sh" --hostname "${HOSTNAME_ARG}"

# ── 7. Run Sherlock setup ─────────────────────────────────────────────────────
hdr "7. Sherlock setup"
step "Running setup (you will be prompted for config)..."
bash "${DEST}/setup.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}Sherlock restored successfully.${RESET}"
echo ""
LAN_IP=$(ifconfig 2>/dev/null | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | head -1)
echo "  Access Sherlock at:"
echo "    https://localhost"
[[ -n "${LAN_IP:-}" ]] && echo "    https://${LAN_IP}  (LAN)"
echo ""
echo "  First run: create admin user"
echo "    ${DEST}/venv/bin/python ${DEST}/web/create_admin.py"
echo ""
RESTORE_EOF

chmod +x "${BUNDLE_DIR}/restore.sh"
ok "restore.sh generated"

# ── 8. Write manifest ────────────────────────────────────────────────────────
hdr "8. Writing manifest"
PYTHON_VER=$(python3 --version 2>&1)
cat > "${BUNDLE_DIR}/MANIFEST.txt" <<EOF
Sherlock Air-Gap Bundle
Generated: ${TIMESTAMP}
Built on:  $(uname -srm)
Python:    ${PYTHON_VER}

Contents:
  docker-images.tar.gz   — Ollama + ChromaDB Docker images
  ollama-models/         — Pre-pulled Ollama model weights
  wheelhouse/            — Python pip packages (offline install)
  whisper-models/        — faster-whisper model files
  homebrew-bottles/      — Nginx + deps (Homebrew bottles)
  sherlock-source/       — Sherlock source code
  restore.sh             — Run this to deploy on target machine

Restore:
  1. Extract: tar -xzf sherlock-bundle-*.tar.gz
  2. Run:     cd sherlock-bundle && ./restore.sh

Options:
  ./restore.sh --hostname sherlock.lawfirm.local
  ./restore.sh --dest /opt/sherlock
EOF
ok "Manifest written"

# ── 9. Package ───────────────────────────────────────────────────────────────
hdr "9. Creating final archive"
step "Compressing bundle ($(size "${BUNDLE_DIR}") uncompressed)..."
step "This may take several minutes..."

BUNDLE_PARENT=$(dirname "${BUNDLE_DIR}")
BUNDLE_NAME=$(basename "${BUNDLE_DIR}")
tar -czf "${OUTPUT}" -C "${BUNDLE_PARENT}" "${BUNDLE_NAME}"

ok "Bundle created: ${OUTPUT} ($(size "${OUTPUT}"))"

# Cleanup staging
rm -rf "${BUNDLE_DIR}"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}Bundle complete!${RESET}"
echo ""
echo "  File   : ${OUTPUT}"
echo "  Size   : $(size "${OUTPUT}")"
echo ""
echo "  To deploy on target machine:"
echo "    1. Copy ${OUTPUT} to USB drive"
echo "    2. On target: tar -xzf $(basename "${OUTPUT}")"
echo "    3. On target: cd sherlock-bundle && ./restore.sh"
echo ""
echo "  Log saved to: ${LOG}"
