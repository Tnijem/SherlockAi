#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Sherlock AI — OFFLINE Installer (runs on NUC after Ubuntu install)
# ═══════════════════════════════════════════════════════════════════════════════
# Usage:
#   1. Mount Ventoy USB:  sudo mkdir -p /mnt/usb && sudo mount /dev/sda1 /mnt/usb
#   2. cd /mnt/usb/sherlock-bundle
#   3. chmod +x install.sh && ./install.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHERLOCK_DIR="${HOME}/Sherlock"
VENV="${SHERLOCK_DIR}/venv"
LOG_DIR="${SHERLOCK_DIR}/logs"
LOG="${LOG_DIR}/install-$(date +%Y%m%d-%H%M%S).log"

OLLAMA_LLM_MODEL="gemma3:12b"
OLLAMA_EMBED_MODEL="mxbai-embed-large"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*" | tee -a "${LOG}"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*" | tee -a "${LOG}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*" | tee -a "${LOG}"; }
hdr()  { echo -e "\n${BOLD}${BLUE}══  $*  ══${RESET}" | tee -a "${LOG}"; }
fail() { echo -e "  ${RED}✗  ERROR: $*${RESET}"; exit 1; }

mkdir -p "${LOG_DIR}"
echo "Sherlock Offline Install — $(date)" > "${LOG}"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗"
echo -e "║     SHERLOCK AI — OFFLINE Installer          ║"
echo -e "║     No internet required                     ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""

[[ "${EUID}" -eq 0 ]] && fail "Run as the sherlock user, not root."
sudo -v || fail "No sudo access."

# ── 1. System packages ────────────────────────────────────────────────────────
hdr "Step 1: System Packages (offline)"

APT_CACHE="${BUNDLE_DIR}/apt-packages"

if [[ -d "${APT_CACHE}" ]] && ls "${APT_CACHE}"/*.deb &>/dev/null; then
  step "Installing from local .deb cache..."

  REPO_DIR="/tmp/sherlock-apt-repo"
  sudo mkdir -p "${REPO_DIR}"
  sudo cp "${APT_CACHE}"/*.deb "${REPO_DIR}/"
  (cd "${REPO_DIR}" && sudo dpkg-scanpackages . /dev/null | sudo gzip -9c | sudo tee Packages.gz > /dev/null)

  echo "deb [trusted=yes] file://${REPO_DIR} ./" | \
    sudo tee /etc/apt/sources.list.d/sherlock-local.list > /dev/null

  sudo apt-get update -qq 2>/dev/null || true
  sudo apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    git curl wget unzip build-essential \
    nginx \
    tesseract-ocr tesseract-ocr-eng \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    ffmpeg \
    ufw jq sqlite3 libpq-dev \
    2>>"${LOG}" || warn "Some packages may have failed — continuing"

  ok "System packages installed from local cache"
else
  warn "No apt-packages folder found — attempting online install"
  sudo apt-get update -qq >> "${LOG}" 2>&1
  sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    git nginx tesseract-ocr tesseract-ocr-eng \
    libreoffice-writer libreoffice-calc ffmpeg \
    ufw jq sqlite3 2>>"${LOG}" || warn "Some packages failed"
fi

# ── 2. Ollama ─────────────────────────────────────────────────────────────────
hdr "Step 2: Ollama (offline)"

OLLAMA_BIN="${BUNDLE_DIR}/ollama/ollama-linux-amd64"

if command -v ollama &>/dev/null; then
  ok "Ollama already installed"
else
  if [[ -f "${OLLAMA_BIN}" ]]; then
    step "Installing Ollama from local binary..."
    sudo cp "${OLLAMA_BIN}" /usr/local/bin/ollama
    sudo chmod +x /usr/local/bin/ollama
  else
    warn "Ollama binary not in bundle — downloading..."
    curl -fsSL https://ollama.com/install.sh | sh >> "${LOG}" 2>&1 || fail "Ollama install failed"
  fi

  sudo useradd -r -s /bin/false -m -d /usr/share/ollama ollama 2>/dev/null || true
  sudo usermod -aG ollama "${USER}"

  sudo tee /etc/systemd/system/ollama.service > /dev/null << 'OLLAMASVC'
[Unit]
Description=Ollama Service
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ollama serve
User=ollama
Group=ollama
Restart=always
RestartSec=3
Environment="HOME=/usr/share/ollama"
Environment="OLLAMA_HOST=127.0.0.1:11434"

[Install]
WantedBy=multi-user.target
OLLAMASVC

  sudo systemctl daemon-reload
  sudo systemctl enable ollama >> "${LOG}" 2>&1
  sudo systemctl start ollama  >> "${LOG}" 2>&1
  ok "Ollama installed and started"
fi

# ── 3. AI Models ──────────────────────────────────────────────────────────────
hdr "Step 3: AI Models (offline)"

MODELS_SRC="${BUNDLE_DIR}/ollama/models"
OLLAMA_HOME="/usr/share/ollama/.ollama"

if [[ -d "${MODELS_SRC}" ]]; then
  step "Restoring model blobs..."
  sudo mkdir -p "${OLLAMA_HOME}"
  sudo rsync -a "${MODELS_SRC}/" "${OLLAMA_HOME}/models/" >> "${LOG}" 2>&1
  sudo chown -R ollama:ollama "${OLLAMA_HOME}"
  sudo systemctl restart ollama >> "${LOG}" 2>&1
  sleep 4

  for i in $(seq 1 20); do
    if ollama list 2>/dev/null | grep -qE "gemma|embed|llama"; then
      ok "Models ready: $(ollama list 2>/dev/null | grep -v NAME | awk '{print $1}' | tr '\n' ' ')"
      break
    fi
    sleep 2
    [[ $i -eq 20 ]] && warn "Models not appearing yet — run: sudo systemctl restart ollama"
  done
else
  warn "No model blobs in bundle — will pull on first use (requires internet)"
fi

# ── 4. Sherlock source ────────────────────────────────────────────────────────
hdr "Step 4: Sherlock Source Code"

SHERLOCK_ARCHIVE="${BUNDLE_DIR}/sherlock-source.tar.gz"

if [[ -f "${SHERLOCK_DIR}/web/main.py" ]]; then
  ok "Sherlock source already present — skipping extract"
elif [[ -f "${SHERLOCK_ARCHIVE}" ]]; then
  step "Extracting Sherlock source to ${SHERLOCK_DIR}..."
  mkdir -p "${SHERLOCK_DIR}"
  tar -xzf "${SHERLOCK_ARCHIVE}" -C "${SHERLOCK_DIR}/" >> "${LOG}" 2>&1
  ok "Sherlock source extracted"
else
  fail "No sherlock-source.tar.gz found in bundle"
fi

# ── 5. Python virtual environment ─────────────────────────────────────────────
hdr "Step 5: Python Environment"

if [[ ! -d "${VENV}" ]]; then
  step "Creating Python virtual environment..."
  python3 -m venv "${VENV}" >> "${LOG}" 2>&1
  ok "venv created"
else
  ok "venv already exists"
fi

WHEELS_DIR="${BUNDLE_DIR}/pip-wheels"

if [[ -d "${WHEELS_DIR}" ]] && ls "${WHEELS_DIR}"/*.whl &>/dev/null 2>&1; then
  step "Installing Python packages from local wheel cache..."
  "${VENV}/bin/pip" install --upgrade pip --quiet >> "${LOG}" 2>&1 || true
  "${VENV}/bin/pip" install \
    --no-index \
    --find-links "${WHEELS_DIR}" \
    -r "${SHERLOCK_DIR}/requirements.txt" \
    >> "${LOG}" 2>&1 \
  && ok "Python packages installed from local cache" \
  || {
    warn "Offline wheel install incomplete — trying online fallback..."
    "${VENV}/bin/pip" install -q -r "${SHERLOCK_DIR}/requirements.txt" >> "${LOG}" 2>&1 \
      && ok "Python packages installed (online)"
  }
else
  warn "No pip wheels found — installing online..."
  "${VENV}/bin/pip" install --upgrade pip --quiet >> "${LOG}" 2>&1 || true
  "${VENV}/bin/pip" install -q -r "${SHERLOCK_DIR}/requirements.txt" >> "${LOG}" 2>&1 \
    && ok "Python packages installed"
fi

# ── 6. Directories & config ───────────────────────────────────────────────────
hdr "Step 6: Directories & Configuration"

for dir in uploads chroma logs outputs data models/whisper SampleData; do
  mkdir -p "${SHERLOCK_DIR}/${dir}"
done
ok "Directories created"

CONF="${SHERLOCK_DIR}/sherlock.conf"
if [[ ! -f "${CONF}" ]]; then
  JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  cat > "${CONF}" << CONFEOF
OLLAMA_URL=http://localhost:11434
CHROMA_PATH=${SHERLOCK_DIR}/chroma
DB_PATH=${SHERLOCK_DIR}/data/sherlock.db
OUTPUTS_DIR=${SHERLOCK_DIR}/outputs
UPLOADS_DIR=${SHERLOCK_DIR}/uploads
WHISPER_MODEL_DIR=${SHERLOCK_DIR}/models/whisper
LLM_MODEL=${OLLAMA_LLM_MODEL}
EMBED_MODEL=${OLLAMA_EMBED_MODEL}
WHISPER_MODEL=medium
JWT_SECRET=${JWT_SECRET}
JWT_ALGORITHM=HS256
JWT_EXPIRY_HOURS=8
RATE_LIMIT_RPM=30
RATE_LIMIT_ADMIN_RPM=120
SYSTEM_NAME=Sherlock
NAS_PATHS=
OUTPUT_MIRROR_PATHS=
CONFEOF
  ok "sherlock.conf created"
else
  ok "sherlock.conf already exists (preserved)"
fi

# ── 7. Sherlock systemd service ───────────────────────────────────────────────
hdr "Step 7: Sherlock Web Service"

sudo tee /etc/systemd/system/sherlock.service > /dev/null << SHERLOCKESC
[Unit]
Description=Sherlock AI Web Server
After=network-online.target ollama.service
Wants=ollama.service

[Service]
User=${USER}
Group=${USER}
WorkingDirectory=${SHERLOCK_DIR}
ExecStart=${VENV}/bin/uvicorn web.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment="PATH=${VENV}/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
SHERLOCKESC

sudo systemctl daemon-reload
sudo systemctl enable sherlock >> "${LOG}" 2>&1
sudo systemctl start sherlock  >> "${LOG}" 2>&1
sleep 3

if sudo systemctl is-active --quiet sherlock; then
  ok "Sherlock web server running"
else
  warn "Sherlock service failed to start — check: sudo journalctl -u sherlock -n 50"
fi

# ── 8. Firewall ───────────────────────────────────────────────────────────────
hdr "Step 8: Firewall"
sudo ufw allow ssh    >> "${LOG}" 2>&1 || true
sudo ufw allow 8000   >> "${LOG}" 2>&1 || true
sudo ufw --force enable >> "${LOG}" 2>&1 || true
ok "Firewall configured (SSH + port 8000)"

# ── Done ──────────────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
echo -e "║     SHERLOCK AI — Install Complete!          ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Open Sherlock:${RESET}  http://${IP}:8000"
echo -e "  ${BOLD}Log file:${RESET}       ${LOG}"
echo ""
echo -e "  ${YELLOW}First time: create your admin account at the URL above.${RESET}"
echo ""
echo -e "  To manage the service:"
echo -e "    sudo systemctl status sherlock"
echo -e "    sudo systemctl restart sherlock"
echo -e "    sudo journalctl -u sherlock -f"
echo ""
