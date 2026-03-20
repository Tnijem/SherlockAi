#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Sherlock — Offline Bundle Creator (run on Mac)
# ═══════════════════════════════════════════════════════════════════════════════
# This script downloads EVERYTHING the NUC needs to install Sherlock
# with NO internet connection. Run this on your Mac, then copy the
# bundle folder to your Ventoy USB drive.
#
# Requirements (on Mac):
#   • Docker Desktop running
#   • ~15 GB free space (for download staging)
#   • ~10 GB on Ventoy USB (after compression)
#   • Ollama models already pulled locally (gemma3:4b + mxbai-embed-large)
#
# Usage:
#   chmod +x bundle-for-nuc.sh
#   ./bundle-for-nuc.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
UBUNTU_VERSION="24.04.4"
UBUNTU_ISO="ubuntu-${UBUNTU_VERSION}-live-server-amd64.iso"
UBUNTU_URL="https://releases.ubuntu.com/${UBUNTU_VERSION}/${UBUNTU_ISO}"

OLLAMA_VERSION="0.9.4"   # latest as of writing — update if needed
OLLAMA_LINUX_URL="https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64"

DOCKER_VERSION="28.0.4"
DOCKER_URL="https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz"
DOCKER_COMPOSE_VERSION="v2.35.1"
DOCKER_COMPOSE_URL="https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-linux-x86_64"

LLM_MODEL="gemma3:12b"   # 32 GB RAM — use 12b for better legal reasoning
EMBED_MODEL="mxbai-embed-large"

SHERLOCK_DIR="${HOME}/Sherlock"
BUNDLE_DIR="${SHERLOCK_DIR}/bundle"   # staging area on Mac
BUNDLE_NAME="sherlock-bundle"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
step() { echo -e "  ${BLUE}→${RESET}  $*"; }
hdr()  { echo -e "\n${BOLD}${BLUE}══  $*  ══${RESET}"; }
fail() { echo -e "  ${RED}✗  $*${RESET}"; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗"
echo -e "║     SHERLOCK — Offline Bundle Creator        ║"
echo -e "║     Run on Mac → copy to Ventoy USB          ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""

# Docker must be running
docker info >/dev/null 2>&1 || fail "Docker Desktop is not running. Start it and retry."
ok "Docker Desktop is running"

# Ollama models must be present
OLLAMA_MODELS_DIR="${HOME}/.ollama/models"
for model_check in "${LLM_MODEL}" "${EMBED_MODEL}"; do
  model_blob=$(ls "${OLLAMA_MODELS_DIR}/manifests/registry.ollama.ai/library" 2>/dev/null | \
               grep -i "$(echo "${model_check}" | cut -d: -f1)" | head -1 || true)
  if [[ -z "${model_blob}" ]]; then
    warn "Ollama model '${model_check}' not found in local cache."
    echo ""
    echo -e "  Pull it first:  ${BOLD}ollama pull ${model_check}${RESET}"
    echo ""
    fail "Missing model: ${model_check}"
  fi
  ok "Model present: ${model_check}"
done

# Disk space check — use diskutil on macOS (df undercounts APFS purgeable space)
if command -v diskutil &>/dev/null; then
  FREE_GB=$(diskutil info / | awk '/Free Space|Available Space/{gsub(/[^0-9.]/,"",$NF); printf "%d", $NF+0; exit}')
else
  FREE_GB=$(df -k "${HOME}" | awk 'NR==2{printf "%d", $4/1024/1024}')
fi
[[ "${FREE_GB:-99}" -lt 15 ]] && warn "Less than 15 GB free (${FREE_GB} GB). Download may fail."

mkdir -p "${BUNDLE_DIR}"

# ── Step 1: Ubuntu ISO ────────────────────────────────────────────────────────
hdr "Step 1: Ubuntu Server ISO"

ISO_DEST="${BUNDLE_DIR}/${UBUNTU_ISO}"
if [[ -f "${ISO_DEST}" ]]; then
  ok "ISO already downloaded: ${UBUNTU_ISO}"
else
  step "Downloading Ubuntu Server ${UBUNTU_VERSION} (~2.5 GB)..."
  curl -L --progress-bar -o "${ISO_DEST}" "${UBUNTU_URL}"
  ok "Downloaded: ${UBUNTU_ISO}"
fi

# ── Step 2: Ollama Linux binary ───────────────────────────────────────────────
hdr "Step 2: Ollama Linux Binary"

mkdir -p "${BUNDLE_DIR}/ollama"
OLLAMA_DEST="${BUNDLE_DIR}/ollama/ollama-linux-amd64"
if [[ -f "${OLLAMA_DEST}" ]]; then
  ok "Ollama binary already downloaded"
else
  step "Downloading Ollama ${OLLAMA_VERSION} Linux binary (~60 MB)..."
  curl -L --progress-bar -o "${OLLAMA_DEST}" "${OLLAMA_LINUX_URL}"
  chmod +x "${OLLAMA_DEST}"
  ok "Ollama Linux binary downloaded"
fi

# ── Step 3: Copy Ollama models ────────────────────────────────────────────────
hdr "Step 3: Ollama Models (from local cache)"

MODELS_DEST="${BUNDLE_DIR}/ollama/models"
mkdir -p "${MODELS_DEST}"

step "Copying model blobs (this may take a few minutes)..."
# Copy entire models directory — blobs are content-addressed so cross-platform
# Use cp -R instead of rsync to avoid macOS rsync version incompatibilities
cp -R "${OLLAMA_MODELS_DIR}/." "${MODELS_DEST}/"
ok "Model blobs copied (~$(du -sh "${MODELS_DEST}" | cut -f1))"

# ── Step 4: Docker CE static binaries ────────────────────────────────────────
hdr "Step 4: Docker CE (static binaries)"

DOCKER_DEST="${BUNDLE_DIR}/docker"
mkdir -p "${DOCKER_DEST}"

if [[ -f "${DOCKER_DEST}/docker-${DOCKER_VERSION}.tgz" ]]; then
  ok "Docker tgz already downloaded"
else
  step "Downloading Docker CE ${DOCKER_VERSION} static binaries (~70 MB)..."
  curl -L --progress-bar -o "${DOCKER_DEST}/docker-${DOCKER_VERSION}.tgz" "${DOCKER_URL}"
  ok "Docker CE downloaded"
fi

if [[ -f "${DOCKER_DEST}/docker-compose" ]]; then
  ok "Docker Compose already downloaded"
else
  step "Downloading Docker Compose ${DOCKER_COMPOSE_VERSION}..."
  curl -L --progress-bar -o "${DOCKER_DEST}/docker-compose" "${DOCKER_COMPOSE_URL}"
  chmod +x "${DOCKER_DEST}/docker-compose"
  ok "Docker Compose downloaded"
fi

# ── Step 5: System apt packages ───────────────────────────────────────────────
hdr "Step 5: System apt Packages"
# Use an Ubuntu 24.04 Docker container to download all .deb files

APT_DEST="${BUNDLE_DIR}/apt-packages"
mkdir -p "${APT_DEST}"

APT_PACKAGE_COUNT=$(ls "${APT_DEST}"/*.deb 2>/dev/null | wc -l || echo 0)
if [[ "${APT_PACKAGE_COUNT}" -gt 50 ]]; then
  ok "apt packages already downloaded (${APT_PACKAGE_COUNT} .deb files)"
else
  step "Downloading apt packages using Ubuntu 24.04 Docker container..."
  step "(This pulls all system dependencies for Linux x86_64 — ~300 MB)"

  docker run --rm \
    --platform linux/amd64 \
    -v "${APT_DEST}:/apt-cache" \
    ubuntu:24.04 \
    bash -c '
      set -e
      apt-get update -qq

      # Download packages + all dependencies into /apt-cache
      cd /apt-cache

      apt-get install -y --download-only -qq \
        python3 python3-pip python3-venv python3-dev \
        git curl wget unzip build-essential \
        nginx \
        tesseract-ocr tesseract-ocr-eng \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        ffmpeg \
        ca-certificates gnupg lsb-release \
        ufw jq sqlite3 libpq-dev \
        linux-firmware \
        2>/dev/null
      # Note: ROCm (AMD GPU) packages are large (~2 GB) and must be downloaded
      # separately from repo.radeon.com — handled by installer at runtime if
      # internet is available, or pre-bundled via bundle-rocm-addon.sh

      # Copy downloaded .deb files to our cache directory
      cp /var/cache/apt/archives/*.deb /apt-cache/ 2>/dev/null || true

      echo "Downloaded $(ls /apt-cache/*.deb 2>/dev/null | wc -l) packages"
    '

  APT_COUNT=$(ls "${APT_DEST}"/*.deb 2>/dev/null | wc -l || echo 0)
  ok "Downloaded ${APT_COUNT} .deb packages"
fi

# ── Step 6: Python wheels (Linux x86_64) ──────────────────────────────────────
hdr "Step 6: Python Packages (Linux wheels)"

WHEELS_DEST="${BUNDLE_DIR}/pip-wheels"
mkdir -p "${WHEELS_DEST}"

WHEEL_COUNT=$(ls "${WHEELS_DEST}"/*.whl 2>/dev/null | wc -l || echo 0)
if [[ "${WHEEL_COUNT}" -gt 20 ]]; then
  ok "Python wheels already downloaded (${WHEEL_COUNT} wheels)"
else
  step "Downloading Python wheels for Linux x86_64 / Python 3.12..."
  step "(Using pip's platform override to get Linux-compatible wheels)"

  # Use Docker to ensure we get the right wheels for Linux Python 3.12
  docker run --rm \
    --platform linux/amd64 \
    -v "${WHEELS_DEST}:/wheels" \
    -v "${SHERLOCK_DIR}/requirements.txt:/requirements.txt:ro" \
    python:3.12-slim \
    bash -c '
      set -e
      pip install --upgrade pip -q
      pip download \
        --only-binary :all: \
        --dest /wheels \
        -r /requirements.txt \
        -q
      echo "Downloaded $(ls /wheels/*.whl 2>/dev/null | wc -l) wheels"
    '

  WHEEL_COUNT=$(ls "${WHEELS_DEST}"/*.whl 2>/dev/null | wc -l || echo 0)
  ok "Downloaded ${WHEEL_COUNT} Python wheels"
fi

# ── Step 7: Docker images (ChromaDB + SearXNG) ────────────────────────────────
hdr "Step 7: Docker Images (ChromaDB + SearXNG)"

IMAGES_DEST="${BUNDLE_DIR}/docker-images"
mkdir -p "${IMAGES_DEST}"

save_image() {
  local image="$1"
  local filename="$2"
  if [[ -f "${IMAGES_DEST}/${filename}" ]]; then
    ok "Image already saved: ${filename}"
    return
  fi
  step "Pulling and saving ${image}..."
  docker pull --platform linux/amd64 "${image}" >/dev/null 2>&1
  docker save "${image}" | gzip > "${IMAGES_DEST}/${filename}"
  ok "Saved: ${filename} (~$(du -sh "${IMAGES_DEST}/${filename}" | cut -f1))"
}

save_image "chromadb/chroma:latest"     "chroma.tar.gz"
save_image "searxng/searxng:latest"     "searxng.tar.gz"

# ── Step 8: Sherlock source code ──────────────────────────────────────────────
hdr "Step 8: Sherlock Source Code"

SHERLOCK_ARCHIVE="${BUNDLE_DIR}/sherlock-source.tar.gz"
step "Creating Sherlock source archive..."
# Exclude venv, bundle itself, logs, uploads, outputs, db
tar -czf "${SHERLOCK_ARCHIVE}" \
  -C "${HOME}" \
  --exclude="Sherlock/venv" \
  --exclude="Sherlock/bundle" \
  --exclude="Sherlock/ollama" \
  --exclude="Sherlock/chromadb" \
  --exclude="Sherlock/logs" \
  --exclude="Sherlock/uploads" \
  --exclude="Sherlock/outputs" \
  --exclude="Sherlock/data" \
  --exclude="Sherlock/mnts" \
  --exclude="Sherlock/.git" \
  Sherlock/
ok "Source archive created (~$(du -sh "${SHERLOCK_ARCHIVE}" | cut -f1))"

# ── Step 9: Copy autoinstall files ───────────────────────────────────────────
hdr "Step 9: Autoinstall Files"

cp -r "${SHERLOCK_DIR}/deploy/autoinstall" "${BUNDLE_DIR}/"
ok "Autoinstall config copied"

# ── Step 10: Write offline installer ─────────────────────────────────────────
hdr "Step 10: Writing Offline Installer"

cat > "${BUNDLE_DIR}/install.sh" << 'OFFLINE_INSTALLER'
#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Sherlock — OFFLINE Installer (runs on NUC after Ubuntu install)
# ═══════════════════════════════════════════════════════════════════════════════
# Usage:
#   1. Mount Ventoy USB (or copy sherlock-bundle/ to ~/setup/)
#   2. cd /path/to/sherlock-bundle
#   3. chmod +x install.sh && ./install.sh
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHERLOCK_DIR="${HOME}/Sherlock"
VENV="${SHERLOCK_DIR}/venv"
LOG_DIR="${SHERLOCK_DIR}/logs"
LOG="${LOG_DIR}/install-$(date +%Y%m%d-%H%M%S).log"

OLLAMA_LLM_MODEL="gemma3:12b"   # 32 GB RAM — use 12b for better legal reasoning
OLLAMA_EMBED_MODEL="mxbai-embed-large"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*" | tee -a "${LOG}"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*" | tee -a "${LOG}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*" | tee -a "${LOG}"; }
hdr()  { echo -e "\n${BOLD}${BLUE}══  $*  ══${RESET}" | tee -a "${LOG}"; }
fail() { echo -e "  ${RED}✗  $*${RESET}"; exit 1; }

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

# ── 1. System packages from local .deb cache ─────────────────────────────────
hdr "Step 1: System Packages (offline)"

APT_CACHE="${BUNDLE_DIR}/apt-packages"

if [[ -d "${APT_CACHE}" ]] && ls "${APT_CACHE}"/*.deb &>/dev/null; then
  step "Installing from local .deb cache..."

  # Create a temporary local apt repo
  REPO_DIR="/tmp/sherlock-apt-repo"
  sudo mkdir -p "${REPO_DIR}"
  sudo cp "${APT_CACHE}"/*.deb "${REPO_DIR}/"
  (cd "${REPO_DIR}" && sudo dpkg-scanpackages . /dev/null | sudo gzip -9c > Packages.gz)

  # Add local repo to sources
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
    ufw jq sqlite3 >> "${LOG}" 2>&1
fi

# ── 2. Docker CE from static binaries ────────────────────────────────────────
hdr "Step 2: Docker CE (offline)"

DOCKER_TGZ=$(ls "${BUNDLE_DIR}/docker/"docker-*.tgz 2>/dev/null | head -1)

if command -v docker &>/dev/null; then
  ok "Docker already installed: $(docker --version)"
elif [[ -n "${DOCKER_TGZ}" ]]; then
  step "Installing Docker from static binaries..."
  DOCKER_TMP=$(mktemp -d)
  tar -xzf "${DOCKER_TGZ}" -C "${DOCKER_TMP}"
  sudo cp "${DOCKER_TMP}/docker/"* /usr/local/bin/
  rm -rf "${DOCKER_TMP}"

  # Install Docker Compose plugin
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo cp "${BUNDLE_DIR}/docker/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose

  # Create Docker system service
  sudo tee /etc/systemd/system/docker.service > /dev/null << 'DOCKERSVC'
[Unit]
Description=Docker Application Container Engine
After=network-online.target firewalld.service containerd.service time-set.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutStartSec=0
RestartSec=2
Restart=always
StartLimitBurst=3
StartLimitInterval=60s
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity

[Install]
WantedBy=multi-user.target
DOCKERSVC

  sudo tee /etc/systemd/system/docker.socket > /dev/null << 'DOCKERSOCK'
[Unit]
Description=Docker Socket for the API
PartOf=docker.service

[Socket]
ListenStream=/var/run/docker.sock
SocketMode=0660
SocketUser=root
SocketGroup=docker

[Install]
WantedBy=sockets.target
DOCKERSOCK

  sudo groupadd docker 2>/dev/null || true
  sudo usermod -aG docker "${USER}"
  sudo mkdir -p /var/lib/docker
  sudo systemctl daemon-reload
  sudo systemctl enable docker.socket docker.service >> "${LOG}" 2>&1
  sudo systemctl start docker.socket docker.service  >> "${LOG}" 2>&1
  ok "Docker CE installed from static binaries"
else
  fail "Docker binary not found in bundle and docker is not installed."
fi

# ── 3. Load Docker images ─────────────────────────────────────────────────────
hdr "Step 3: Docker Images (ChromaDB + SearXNG)"

for img_file in "${BUNDLE_DIR}/docker-images/"*.tar.gz; do
  [[ -f "${img_file}" ]] || continue
  img_name=$(basename "${img_file}" .tar.gz)
  step "Loading Docker image: ${img_name}..."
  gunzip -c "${img_file}" | sudo docker load >> "${LOG}" 2>&1
  ok "Loaded: ${img_name}"
done

# ── 4. Ollama from local binary ───────────────────────────────────────────────
hdr "Step 4: Ollama (offline)"

OLLAMA_BIN="${BUNDLE_DIR}/ollama/ollama-linux-amd64"

if command -v ollama &>/dev/null; then
  ok "Ollama already installed"
else
  step "Installing Ollama from local binary..."
  sudo cp "${OLLAMA_BIN}" /usr/local/bin/ollama
  sudo chmod +x /usr/local/bin/ollama

  # Create ollama user and service
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
WantedBy=default.target
OLLAMASVC

  sudo systemctl daemon-reload
  sudo systemctl enable ollama >> "${LOG}" 2>&1
  sudo systemctl start ollama  >> "${LOG}" 2>&1
  ok "Ollama installed and started"
fi

# ── 5. Restore Ollama model blobs ─────────────────────────────────────────────
hdr "Step 5: AI Models (offline)"

MODELS_SRC="${BUNDLE_DIR}/ollama/models"
OLLAMA_HOME="/usr/share/ollama/.ollama"

if [[ -d "${MODELS_SRC}" ]]; then
  step "Restoring model blobs to Ollama model directory..."
  sudo mkdir -p "${OLLAMA_HOME}"
  sudo rsync -a "${MODELS_SRC}/" "${OLLAMA_HOME}/models/"
  sudo chown -R ollama:ollama "${OLLAMA_HOME}"
  ok "Model blobs restored"

  # Restart Ollama to pick up models
  sudo systemctl restart ollama >> "${LOG}" 2>&1
  sleep 3

  # Verify
  for i in $(seq 1 20); do
    if ollama list 2>/dev/null | grep -q "embed\|gemma\|llama"; then
      ok "Models available: $(ollama list 2>/dev/null | grep -v NAME | awk '{print $1}' | tr '\n' ' ')"
      break
    fi
    sleep 2
    [[ $i -eq 20 ]] && warn "Models not appearing — may need: sudo systemctl restart ollama"
  done
else
  warn "No model blobs found in bundle — will pull on first use (requires internet)"
fi

# ── 6. Sherlock source code ───────────────────────────────────────────────────
hdr "Step 6: Sherlock Source Code"

SHERLOCK_ARCHIVE="${BUNDLE_DIR}/sherlock-source.tar.gz"

if [[ -d "${SHERLOCK_DIR}/.git" ]]; then
  ok "Sherlock already installed — skipping extract"
elif [[ -f "${SHERLOCK_ARCHIVE}" ]]; then
  step "Extracting Sherlock source..."
  tar -xzf "${SHERLOCK_ARCHIVE}" -C "${HOME}/"
  ok "Sherlock extracted to ${SHERLOCK_DIR}"
else
  fail "No sherlock-source.tar.gz found in bundle"
fi

# ── 7. Python virtual environment ────────────────────────────────────────────
hdr "Step 7: Python Environment (offline)"

WHEELS_DIR="${BUNDLE_DIR}/pip-wheels"

if [[ ! -d "${VENV}" ]]; then
  step "Creating Python virtual environment..."
  python3 -m venv "${VENV}" >> "${LOG}" 2>&1
  ok "venv created"
fi

if [[ -d "${WHEELS_DIR}" ]] && ls "${WHEELS_DIR}"/*.whl &>/dev/null; then
  step "Installing Python packages from local wheel cache..."
  "${VENV}/bin/pip" install --upgrade pip --no-index \
    --find-links "${WHEELS_DIR}" pip >> "${LOG}" 2>&1 || \
  "${VENV}/bin/pip" install --upgrade pip -q >> "${LOG}" 2>&1

  "${VENV}/bin/pip" install \
    --no-index \
    --find-links "${WHEELS_DIR}" \
    -r "${SHERLOCK_DIR}/requirements.txt" \
    >> "${LOG}" 2>&1
  ok "Python packages installed from local cache"
else
  warn "No pip wheels found — attempting online install"
  "${VENV}/bin/pip" install -q -r "${SHERLOCK_DIR}/requirements.txt" >> "${LOG}" 2>&1
fi

# ── 8-14: Same as online installer ───────────────────────────────────────────
# (Directories, sherlock.conf, Docker Compose, systemd, Nginx, Firewall, DB)
# Source the shared config steps:

SHERLOCK_INSTALL="${SHERLOCK_DIR}/deploy/sherlock-install.sh"
if [[ -f "${SHERLOCK_INSTALL}" ]]; then
  # Run only the configuration/setup steps (skip download steps)
  export SKIP_DOWNLOADS=1
  bash "${SHERLOCK_INSTALL}" --configure-only
else
  warn "sherlock-install.sh not found — running inline config"
  # Inline fallback: create directories and config
  for dir in "${SHERLOCK_DIR}/data" "${SHERLOCK_DIR}/logs" \
              "${SHERLOCK_DIR}/uploads" "${SHERLOCK_DIR}/outputs" \
              "${SHERLOCK_DIR}/models/whisper" "${SHERLOCK_DIR}/SampleData"; do
    mkdir -p "${dir}"
  done

  CONF="${SHERLOCK_DIR}/sherlock.conf"
  if [[ ! -f "${CONF}" ]]; then
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > "${CONF}" << CONFEOF
OLLAMA_URL=http://localhost:11434
CHROMA_URL=http://localhost:8000
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
  fi
fi

IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗"
echo -e "║       SHERLOCK OFFLINE INSTALL COMPLETE!         ║"
echo -e "╚══════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Access at: ${BOLD}https://${IP}${RESET}"
echo -e "  Log: ${LOG}"
echo ""
OFFLINE_INSTALLER

chmod +x "${BUNDLE_DIR}/install.sh"
ok "Offline installer written"

# ── Final: Bundle summary ──────────────────────────────────────────────────────
hdr "Bundle Complete"

BUNDLE_SIZE=$(du -sh "${BUNDLE_DIR}" | cut -f1)

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
echo -e "║          BUNDLE CREATED SUCCESSFULLY!               ║"
echo -e "╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Bundle location:  ${BOLD}${BUNDLE_DIR}${RESET}"
echo -e "  Total size:       ${BOLD}${BUNDLE_SIZE}${RESET}"
echo ""
echo -e "${BOLD}Next steps:${RESET}"
echo ""
echo -e "  1. Copy the entire ${BUNDLE_NAME} folder to your Ventoy USB:"
echo -e "     ${BOLD}rsync -a --progress ${BUNDLE_DIR}/ /Volumes/<YourVentoyUSB>/${BUNDLE_NAME}/${RESET}"
echo ""
echo -e "  2. Also copy the Ubuntu ISO to the Ventoy USB root:"
echo -e "     ${BOLD}cp ${BUNDLE_DIR}/${UBUNTU_ISO} /Volumes/<YourVentoyUSB>/${RESET}"
echo ""
echo -e "  3. Copy autoinstall files:"
echo -e "     ${BOLD}cp -r ${BUNDLE_DIR}/autoinstall /Volumes/<YourVentoyUSB>/sherlock-autoinstall/${RESET}"
echo ""
echo -e "  4. Boot the NUC from Ventoy, install Ubuntu, then:"
echo -e "     ${BOLD}cd /path/to/${BUNDLE_NAME} && ./install.sh${RESET}"
echo ""
echo -e "  See ${BOLD}deploy/README.md${RESET} for full instructions."
echo ""
