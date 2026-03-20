#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Sherlock — Linux Installer (Ubuntu 24.04 LTS / Intel NUC)
# ═══════════════════════════════════════════════════════════════════════════════
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Tnijem/SherlockAi/main/deploy/sherlock-install.sh | bash
#   — or —
#   chmod +x sherlock-install.sh && ./sherlock-install.sh
#
# Idempotent: safe to re-run. Skips steps already completed.
# Run as the 'sherlock' user (or any non-root user with sudo).
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SHERLOCK_REPO="https://github.com/Tnijem/SherlockAi.git"
SHERLOCK_BRANCH="main"
SHERLOCK_DIR="${HOME}/Sherlock"
VENV="${SHERLOCK_DIR}/venv"
LOG_DIR="${SHERLOCK_DIR}/logs"
LOG="${LOG_DIR}/install-$(date +%Y%m%d-%H%M%S).log"

# With 32 GB RAM, run the 12b model for much better legal reasoning
OLLAMA_LLM_MODEL="gemma3:12b"
OLLAMA_EMBED_MODEL="mxbai-embed-large"

# ── Hardware: GEEKOM A8 Max / AMD Ryzen 7 8745HS + Radeon 780M ────────────────
# ROCm (AMD GPU) acceleration for Ollama — significant speedup over CPU-only
ENABLE_ROCM=true

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*" | tee -a "${LOG}"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*" | tee -a "${LOG}"; }
err()  { echo -e "  ${RED}✗${RESET}  $*" | tee -a "${LOG}"; }
hdr()  { echo -e "\n${BOLD}${BLUE}══  $*  ══${RESET}" | tee -a "${LOG}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*" | tee -a "${LOG}"; }

fail() {
  err "$*"
  echo -e "\n${RED}Installation failed. Check log: ${LOG}${RESET}"
  exit 1
}

# ── Preflight ─────────────────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}"
echo "Sherlock Linux Installer — $(date)" > "${LOG}"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗"
echo -e "║       SHERLOCK AI — Linux Installer          ║"
echo -e "║       Ubuntu 24.04 LTS / Intel NUC           ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""

# Must not be root
[[ "${EUID}" -eq 0 ]] && fail "Do not run as root. Run as the 'sherlock' user."

# Confirm sudo works
sudo -v || fail "User $(whoami) does not have sudo access."

# Check Ubuntu version
if ! grep -q "Ubuntu 24" /etc/os-release 2>/dev/null; then
  warn "Not Ubuntu 24 — continuing anyway, but untested."
fi

# ── Step 1: System packages ───────────────────────────────────────────────────
hdr "Step 1: System Packages"

step "Updating package lists..."
sudo apt-get update -qq >> "${LOG}" 2>&1
ok "Package lists updated"

step "Installing system dependencies..."
sudo apt-get install -y -qq \
  python3 python3-pip python3-venv python3-dev \
  git curl wget unzip build-essential \
  nginx \
  tesseract-ocr tesseract-ocr-eng \
  libreoffice-writer libreoffice-calc libreoffice-impress \
  ffmpeg \
  libpq-dev \
  ca-certificates gnupg lsb-release \
  ufw jq sqlite3 \
  linux-firmware \
  >> "${LOG}" 2>&1
ok "System packages installed"

# ── AMD ROCm (GPU acceleration for Ollama on Radeon 780M) ──────────────────
if [[ "${ENABLE_ROCM:-false}" == "true" ]]; then
  hdr "Step 1b: AMD ROCm (GPU Acceleration)"
  if ! command -v rocminfo &>/dev/null; then
    step "Adding AMD ROCm repository..."
    wget -qO - https://repo.radeon.com/rocm/rocm.gpg.key | \
      sudo gpg --dearmor -o /etc/apt/keyrings/rocm.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] \
https://repo.radeon.com/rocm/apt/6.4 noble main" | \
      sudo tee /etc/apt/sources.list.d/rocm.list >> "${LOG}" 2>&1
    sudo apt-get update -qq >> "${LOG}" 2>&1
    sudo apt-get install -y -qq rocm-hip-runtime rocm-opencl-runtime >> "${LOG}" 2>&1
    sudo usermod -aG render,video "${USER}"
    ok "ROCm installed — Ollama will use Radeon 780M GPU"
    warn "GPU group membership takes effect on next login"
  else
    ok "ROCm already installed"
  fi
fi

# Python version check
PY_VER=$(python3 --version 2>&1)
ok "Python: ${PY_VER}"

# ── Step 2: Docker CE ─────────────────────────────────────────────────────────
hdr "Step 2: Docker CE"

if command -v docker &>/dev/null; then
  ok "Docker already installed: $(docker --version)"
else
  step "Installing Docker CE..."
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc >> "${LOG}" 2>&1
  sudo chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list >> "${LOG}" 2>&1

  sudo apt-get update -qq >> "${LOG}" 2>&1
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin >> "${LOG}" 2>&1
  ok "Docker CE installed"
fi

# Add user to docker group
if ! groups "$(whoami)" | grep -q docker; then
  sudo usermod -aG docker "$(whoami)"
  ok "Added $(whoami) to docker group"
  warn "Docker group change takes effect on next login. Using 'newgrp docker' for now."
  # Apply group for rest of this script
  exec newgrp docker << 'DOCKERGROUP'
    echo "Continuing with docker group active..."
DOCKERGROUP
fi

sudo systemctl enable docker >> "${LOG}" 2>&1
sudo systemctl start docker  >> "${LOG}" 2>&1
ok "Docker service enabled and started"

# ── Step 3: Ollama (native, not Docker) ───────────────────────────────────────
hdr "Step 3: Ollama (Native)"

if command -v ollama &>/dev/null; then
  ok "Ollama already installed: $(ollama --version 2>&1 | head -1)"
else
  step "Installing Ollama..."
  curl -fsSL https://ollama.com/install.sh | sudo bash >> "${LOG}" 2>&1
  ok "Ollama installed"
fi

# Enable Ollama as a system service
sudo systemctl enable ollama >> "${LOG}" 2>&1
sudo systemctl start ollama  >> "${LOG}" 2>&1

# Wait for Ollama to come up
step "Waiting for Ollama API..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    ok "Ollama API ready"
    break
  fi
  sleep 2
  [[ $i -eq 30 ]] && fail "Ollama did not start in 60s"
done

# ── Step 4: Clone / Update Sherlock ───────────────────────────────────────────
hdr "Step 4: Sherlock Codebase"

if [[ -d "${SHERLOCK_DIR}/.git" ]]; then
  step "Updating existing Sherlock repo..."
  git -C "${SHERLOCK_DIR}" pull --ff-only >> "${LOG}" 2>&1
  ok "Repo updated"
else
  step "Cloning Sherlock from GitHub..."
  git clone --branch "${SHERLOCK_BRANCH}" "${SHERLOCK_REPO}" "${SHERLOCK_DIR}" >> "${LOG}" 2>&1
  ok "Repo cloned to ${SHERLOCK_DIR}"
fi

# ── Step 5: Python virtual environment ───────────────────────────────────────
hdr "Step 5: Python Environment"

if [[ ! -d "${VENV}" ]]; then
  step "Creating virtual environment..."
  python3 -m venv "${VENV}" >> "${LOG}" 2>&1
  ok "venv created at ${VENV}"
fi

step "Installing Python dependencies..."
"${VENV}/bin/pip" install --upgrade pip -q >> "${LOG}" 2>&1
"${VENV}/bin/pip" install -r "${SHERLOCK_DIR}/requirements.txt" -q >> "${LOG}" 2>&1
ok "Python packages installed"

# ── Step 6: Directory structure ───────────────────────────────────────────────
hdr "Step 6: Directories & Permissions"

for dir in \
  "${SHERLOCK_DIR}/data" \
  "${SHERLOCK_DIR}/logs" \
  "${SHERLOCK_DIR}/uploads" \
  "${SHERLOCK_DIR}/outputs" \
  "${SHERLOCK_DIR}/models/whisper" \
  "${SHERLOCK_DIR}/SampleData" \
  "${SHERLOCK_DIR}/demo"
do
  mkdir -p "${dir}"
done
ok "Directories created"

# ── Step 7: sherlock.conf ─────────────────────────────────────────────────────
hdr "Step 7: Configuration"

CONF="${SHERLOCK_DIR}/sherlock.conf"
if [[ ! -f "${CONF}" ]]; then
  JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  cat > "${CONF}" << EOF
# Sherlock configuration — Linux/NUC deployment
# Generated by installer on $(date)

OLLAMA_URL=http://localhost:11434
CHROMA_URL=http://localhost:8000

DB_PATH=${SHERLOCK_DIR}/data/sherlock.db
OUTPUTS_DIR=${SHERLOCK_DIR}/outputs
UPLOADS_DIR=${SHERLOCK_DIR}/uploads
WHISPER_MODEL_DIR=${SHERLOCK_DIR}/models/whisper

LLM_MODEL=${OLLAMA_LLM_MODEL}
EMBED_MODEL=${OLLAMA_EMBED_MODEL}
WHISPER_MODEL=medium
WHISPER_LANGUAGE=en

RAG_TOP_N=5
MAX_UPLOAD_MB=500

JWT_SECRET=${JWT_SECRET}
JWT_ALGORITHM=HS256
JWT_EXPIRY_HOURS=8

RATE_LIMIT_RPM=30
RATE_LIMIT_ADMIN_RPM=120

SYSTEM_NAME=Sherlock

# NAS paths — add comma-separated read-only source folders here:
NAS_PATHS=

# Mirror paths — comma-separated paths to copy saved outputs:
OUTPUT_MIRROR_PATHS=
EOF
  ok "sherlock.conf created with random JWT secret"
else
  ok "sherlock.conf already exists — skipping"
fi

# ── Step 8: Docker Compose (ChromaDB + SearXNG) ───────────────────────────────
hdr "Step 8: ChromaDB + SearXNG (Docker)"

# SearXNG config
mkdir -p "${SHERLOCK_DIR}/searxng"
if [[ ! -f "${SHERLOCK_DIR}/searxng/settings.yml" ]]; then
  cat > "${SHERLOCK_DIR}/searxng/settings.yml" << 'EOF'
use_default_settings: true
server:
  secret_key: "sherlock-searxng-local-only"
  limiter: false
  image_proxy: false
search:
  safe_search: 0
  autocomplete: ''
engines:
  - name: google
    engine: google
    disabled: false
  - name: bing
    engine: bing
    disabled: false
  - name: duckduckgo
    engine: duckduckgo
    disabled: false
EOF
  ok "SearXNG settings created"
fi

step "Starting ChromaDB and SearXNG..."
cd "${SHERLOCK_DIR}"
docker compose up -d >> "${LOG}" 2>&1

# Wait for ChromaDB
for i in $(seq 1 20); do
  if curl -sf http://localhost:8000/api/v1/heartbeat >/dev/null 2>&1 || \
     curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1; then
    ok "ChromaDB ready"
    break
  fi
  sleep 2
  [[ $i -eq 20 ]] && warn "ChromaDB not responding after 40s — check 'docker ps'"
done

# ── Step 9: systemd services ──────────────────────────────────────────────────
hdr "Step 9: systemd Services"

# — Sherlock Web App —
sudo tee /etc/systemd/system/sherlock-web.service > /dev/null << EOF
[Unit]
Description=Sherlock AI Web Application
After=network.target docker.service ollama.service
Wants=docker.service ollama.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${SHERLOCK_DIR}/web
Environment=HOME=${HOME}
ExecStart=${VENV}/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=append:${LOG_DIR}/web.log
StandardError=append:${LOG_DIR}/web.log

[Install]
WantedBy=multi-user.target
EOF
ok "sherlock-web.service written"

# — Sherlock Indexer (one-shot service + timer) —
sudo tee /etc/systemd/system/sherlock-indexer.service > /dev/null << EOF
[Unit]
Description=Sherlock Document Indexer
After=sherlock-web.service

[Service]
Type=oneshot
User=${USER}
WorkingDirectory=${SHERLOCK_DIR}/web
Environment=HOME=${HOME}
ExecStart=${VENV}/bin/python run_indexer.py
StandardOutput=append:${LOG_DIR}/indexer.log
StandardError=append:${LOG_DIR}/indexer.log
EOF
ok "sherlock-indexer.service written"

sudo tee /etc/systemd/system/sherlock-indexer.timer > /dev/null << EOF
[Unit]
Description=Sherlock Indexer — runs every 30 minutes
Requires=sherlock-indexer.service

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Unit=sherlock-indexer.service

[Install]
WantedBy=timers.target
EOF
ok "sherlock-indexer.timer written"

# — Docker Compose restart on boot —
sudo tee /etc/systemd/system/sherlock-docker.service > /dev/null << EOF
[Unit]
Description=Sherlock Docker Services (ChromaDB + SearXNG)
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=${USER}
WorkingDirectory=${SHERLOCK_DIR}
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=90

[Install]
WantedBy=multi-user.target
EOF
ok "sherlock-docker.service written"

# Enable all services
sudo systemctl daemon-reload >> "${LOG}" 2>&1
sudo systemctl enable sherlock-docker.service  >> "${LOG}" 2>&1
sudo systemctl enable sherlock-web.service     >> "${LOG}" 2>&1
sudo systemctl enable sherlock-indexer.timer   >> "${LOG}" 2>&1
ok "Services enabled"

# ── Step 10: Nginx ────────────────────────────────────────────────────────────
hdr "Step 10: Nginx Reverse Proxy"

# Self-signed cert
CERT_DIR="/etc/nginx/ssl/sherlock"
if [[ ! -f "${CERT_DIR}/sherlock.crt" ]]; then
  sudo mkdir -p "${CERT_DIR}"
  sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/sherlock.key" \
    -out "${CERT_DIR}/sherlock.crt" \
    -subj "/C=US/ST=FL/L=Miami/O=Sherlock AI/CN=sherlock.local" \
    >> "${LOG}" 2>&1
  ok "Self-signed TLS certificate generated (10 years)"
fi

sudo tee /etc/nginx/sites-available/sherlock > /dev/null << 'NGINXCONF'
# HTTP → HTTPS redirect
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

# HTTPS
server {
    listen 443 ssl;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/sherlock/sherlock.crt;
    ssl_certificate_key /etc/nginx/ssl/sherlock/sherlock.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;

    # Max upload size
    client_max_body_size 512M;

    # Proxy to Sherlock
    location / {
        proxy_pass         http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;    # allow long RAG responses
        proxy_send_timeout 300s;
        proxy_buffering    off;     # required for SSE streaming
    }
}
NGINXCONF

# Enable site
sudo ln -sf /etc/nginx/sites-available/sherlock \
            /etc/nginx/sites-enabled/sherlock 2>/dev/null || true
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t >> "${LOG}" 2>&1 && ok "Nginx config valid"
sudo systemctl enable nginx >> "${LOG}" 2>&1
sudo systemctl restart nginx >> "${LOG}" 2>&1
ok "Nginx configured and started"

# ── Step 11: Firewall ─────────────────────────────────────────────────────────
hdr "Step 11: Firewall (ufw)"

sudo ufw allow ssh     >> "${LOG}" 2>&1
sudo ufw allow 80/tcp  >> "${LOG}" 2>&1
sudo ufw allow 443/tcp >> "${LOG}" 2>&1
# Block external access to internal services
sudo ufw deny 3000     >> "${LOG}" 2>&1   # Sherlock direct (use nginx)
sudo ufw deny 8000     >> "${LOG}" 2>&1   # ChromaDB
sudo ufw deny 8888     >> "${LOG}" 2>&1   # SearXNG
sudo ufw deny 11434    >> "${LOG}" 2>&1   # Ollama
sudo ufw --force enable >> "${LOG}" 2>&1
ok "Firewall configured — 80/443/22 open, internal services blocked"

# ── Step 12: Pull Ollama models ───────────────────────────────────────────────
hdr "Step 12: AI Models"

# Check if models already present
pull_if_missing() {
  local model="$1"
  if ollama list 2>/dev/null | grep -q "^${model}"; then
    ok "Model already present: ${model}"
  else
    step "Pulling ${model} (this takes a few minutes)..."
    ollama pull "${model}" >> "${LOG}" 2>&1
    ok "Pulled: ${model}"
  fi
}

pull_if_missing "${OLLAMA_LLM_MODEL}"
pull_if_missing "${OLLAMA_EMBED_MODEL}"

# ── Step 13: Initialize database & admin ──────────────────────────────────────
hdr "Step 13: Database & Admin User"

cd "${SHERLOCK_DIR}/web"

step "Initializing database..."
"${VENV}/bin/python" -c "
import sys; sys.path.insert(0, '.')
from models import init_db
init_db()
print('  Database initialized')
" >> "${LOG}" 2>&1
ok "Database initialized"

# Check if admin exists
ADMIN_EXISTS=$("${VENV}/bin/python" -c "
import sys; sys.path.insert(0, '.')
from models import init_db, SessionLocal, User
init_db()
db = SessionLocal()
admin = db.query(User).filter(User.role=='admin').first()
print('yes' if admin else 'no')
db.close()
" 2>/dev/null)

if [[ "${ADMIN_EXISTS}" == "no" ]]; then
  step "Creating admin user..."
  echo ""
  echo -e "  ${YELLOW}Create the Sherlock admin account:${RESET}"
  "${VENV}/bin/python" create_admin.py
else
  ok "Admin user already exists"
fi

# ── Step 14: Start Sherlock ───────────────────────────────────────────────────
hdr "Step 14: Starting Sherlock"

sudo systemctl start sherlock-docker.service >> "${LOG}" 2>&1
sudo systemctl start sherlock-web.service    >> "${LOG}" 2>&1

# Wait for web app
step "Waiting for Sherlock web app..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:3000 >/dev/null 2>&1; then
    ok "Sherlock web app is running"
    break
  fi
  sleep 2
  [[ $i -eq 30 ]] && warn "Web app not responding — check: sudo journalctl -u sherlock-web -n 50"
done

# ── Done ─────────────────────────────────────────────────────────────────────
IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗"
echo -e "║            SHERLOCK INSTALLED SUCCESSFULLY           ║"
echo -e "╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Access Sherlock at:  ${BOLD}https://${IP}${RESET}"
echo -e "  Also try:            ${BOLD}https://sherlock.local${RESET}  (if mDNS works on your LAN)"
echo ""
echo -e "  Useful commands:"
echo -e "    sudo systemctl status sherlock-web        # App status"
echo -e "    sudo journalctl -u sherlock-web -f        # Live logs"
echo -e "    sudo systemctl restart sherlock-web       # Restart app"
echo -e "    cd ~/Sherlock && ./deploy/sherlock-update.sh  # Update Sherlock"
echo ""
echo -e "  Log file: ${LOG}"
echo ""
echo -e "  ${YELLOW}Note: Your browser will warn about the self-signed certificate."
echo -e "  Click 'Advanced' → 'Proceed' to continue. This is expected.${RESET}"
echo ""
