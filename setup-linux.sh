#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Sherlock setup — Ubuntu / Debian Linux
# Idempotent: safe to re-run. Skips steps already completed.
#
# Usage:
#   ./setup-linux.sh            Full interactive setup
#   ./setup-linux.sh --update   Re-install deps + restart services
#   ./setup-linux.sh --status   Show current service status
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

VERSION="2.0-linux"
SHERLOCK="${HOME}/Sherlock"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG="${SHERLOCK}/logs/setup-${TIMESTAMP}.log"
CONF="${SHERLOCK}/sherlock.conf"
VENV="${SHERLOCK}/venv"
WEB="${SHERLOCK}/web"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*"; }
hdr()  { echo -e "\n${BOLD}${BLUE}$*${RESET}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*"; }

MODE="full"
[[ "${1:-}" == "--update" ]] && MODE="update"
[[ "${1:-}" == "--status" ]] && MODE="status"

mkdir -p "${SHERLOCK}/logs"
exec > >(tee -a "${LOG}") 2>&1

# ── Status ────────────────────────────────────────────────────────────────────
if [[ "${MODE}" == "status" ]]; then
  hdr "Sherlock Status (Linux)"
  echo ""
  for svc in sherlock-ollama sherlock-chroma; do
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${svc}$" \
      && ok "${svc}: running" || err "${svc}: stopped"
  done
  curl -sf http://localhost:3000 >/dev/null 2>&1 \
    && ok "sherlock-web: running (http://localhost:3000)" \
    || err "sherlock-web: stopped"
  curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 \
    && ok "Ollama API: healthy" || warn "Ollama API: not responding"
  curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1 \
    && ok "ChromaDB: healthy" || warn "ChromaDB: not responding"
  [[ -f "${CONF}" ]] && ok "sherlock.conf: present" || warn "sherlock.conf: missing"
  [[ -f "${SHERLOCK}/data/sherlock.db" ]] && ok "sherlock.db: present" || warn "sherlock.db: missing"
  echo ""; exit 0
fi

# ── Header ────────────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${BOLD}  ╔══════════════════════════════════════╗${RESET}"
echo -e "${BOLD}  ║         S H E R L O C K              ║${RESET}"
echo -e "${BOLD}  ║   Legal Intelligence. Offline.       ║${RESET}"
echo -e "${BOLD}  ╚══════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Setup v${VERSION} — $(date)"
echo ""

# ── Detect RAM — recommend model tier ─────────────────────────────────────────
hdr "Detecting hardware"
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$(( TOTAL_RAM_KB / 1024 / 1024 ))
ok "Total RAM: ~${TOTAL_RAM_GB} GB"

if   [[ $TOTAL_RAM_GB -ge 28 ]]; then
  RECOMMENDED_MODEL="sherlock-rag"
  TIER="Standard (32GB+)"
elif [[ $TOTAL_RAM_GB -ge 14 ]]; then
  RECOMMENDED_MODEL="mistral:7b"
  TIER="Mid (16GB)"
else
  RECOMMENDED_MODEL="llama3.2:3b"
  TIER="Compact (8GB)"
fi
ok "Tier detected: ${TIER}"
ok "Recommended LLM: ${RECOMMENDED_MODEL}"

# ── Dependency checks ─────────────────────────────────────────────────────────
hdr "Checking dependencies"
DEPS_OK=true

command -v docker &>/dev/null \
  && ok "Docker: $(docker --version | awk '{print $3}' | tr -d ',')" \
  || { err "Docker not found. Install: sudo apt install docker.io docker-compose-plugin"; DEPS_OK=false; }

command -v python3 &>/dev/null \
  && ok "Python: $(python3 --version)" \
  || { err "Python 3 not found. Install: sudo apt install python3 python3-venv"; DEPS_OK=false; }

[[ "${DEPS_OK}" == "false" ]] && { err "Fix missing dependencies and re-run."; exit 1; }

command -v tesseract &>/dev/null \
  && ok "Tesseract OCR: available" \
  || warn "Tesseract not found. Install: sudo apt install tesseract-ocr"

command -v libreoffice &>/dev/null || command -v soffice &>/dev/null \
  && ok "LibreOffice: available" \
  || warn "LibreOffice not found. Install: sudo apt install libreoffice"

# ── Interactive config ─────────────────────────────────────────────────────────
if [[ "${MODE}" == "full" ]]; then
  hdr "Configuration"
  echo ""

  _conf_get() {
    [[ -f "${CONF}" ]] && grep "^${1}=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${2}"
  }

  read -p "  System name [$(_conf_get SYSTEM_NAME Sherlock)]: " INPUT_NAME
  SYSTEM_NAME="${INPUT_NAME:-$(_conf_get SYSTEM_NAME Sherlock)}"

  echo ""
  read -p "  NAS path(s) [$(_conf_get NAS_PATHS '')]: " INPUT_NAS
  NAS_PATHS="${INPUT_NAS:-$(_conf_get NAS_PATHS '')}"

  echo ""
  read -p "  Outputs directory [$(_conf_get OUTPUTS_DIR "${SHERLOCK}/outputs")]: " INPUT_OUT
  OUTPUTS_DIR="${INPUT_OUT:-$(_conf_get OUTPUTS_DIR "${SHERLOCK}/outputs")}"

  echo ""
  read -p "  Uploads directory [$(_conf_get UPLOADS_DIR "${SHERLOCK}/uploads")]: " INPUT_UPL
  UPLOADS_DIR="${INPUT_UPL:-$(_conf_get UPLOADS_DIR "${SHERLOCK}/uploads")}"

  echo ""
  read -p "  Whisper model [tiny/base/small/medium] [$(_conf_get WHISPER_MODEL base)]: " INPUT_WHISPER
  WHISPER_MODEL="${INPUT_WHISPER:-$(_conf_get WHISPER_MODEL base)}"

  echo ""
  read -p "  LLM model [${RECOMMENDED_MODEL}]: " INPUT_LLM
  LLM_MODEL="${INPUT_LLM:-${RECOMMENDED_MODEL}}"

  EXISTING_SECRET=$(_conf_get JWT_SECRET "")
  if [[ -z "${EXISTING_SECRET}" ]]; then
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    ok "Generated new JWT secret."
  else
    JWT_SECRET="${EXISTING_SECRET}"
    ok "Preserving existing JWT secret."
  fi

  hdr "Writing sherlock.conf"
  cat > "${CONF}" << EOF
SYSTEM_NAME=${SYSTEM_NAME}
NAS_PATHS=${NAS_PATHS}
OUTPUTS_DIR=${OUTPUTS_DIR}
UPLOADS_DIR=${UPLOADS_DIR}
DB_PATH=${SHERLOCK}/data/sherlock.db
OLLAMA_URL=http://localhost:11434
CHROMA_URL=http://localhost:8000
JWT_SECRET=${JWT_SECRET}
JWT_EXPIRY_HOURS=8
EMBED_MODEL=mxbai-embed-large
LLM_MODEL=${LLM_MODEL}
WHISPER_MODEL=${WHISPER_MODEL}
WHISPER_MODEL_DIR=${SHERLOCK}/models/whisper
MAX_UPLOAD_MB=500
RAG_TOP_N=5
EOF
  ok "sherlock.conf written."
else
  LLM_MODEL=$(grep "^LLM_MODEL=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${RECOMMENDED_MODEL}")
  WHISPER_MODEL=$(grep "^WHISPER_MODEL=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "base")
  OUTPUTS_DIR=$(grep "^OUTPUTS_DIR=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${SHERLOCK}/outputs")
  UPLOADS_DIR=$(grep "^UPLOADS_DIR=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${SHERLOCK}/uploads")
fi

# ── Directories ───────────────────────────────────────────────────────────────
hdr "Creating directories"
for DIR in "${SHERLOCK}/data" "${SHERLOCK}/logs" "${SHERLOCK}/models/whisper" \
           "${OUTPUTS_DIR}" "${UPLOADS_DIR}" "${WEB}/static/assets"; do
  mkdir -p "${DIR}" && ok "${DIR}"
done

cp "${SHERLOCK}/branding/graphics/logo.svg"      "${WEB}/static/assets/logo.svg"      2>/dev/null || true
cp "${SHERLOCK}/branding/graphics/app-icon.svg"  "${WEB}/static/assets/favicon.svg"   2>/dev/null || true

# ── Python dependencies ───────────────────────────────────────────────────────
hdr "Python dependencies"
if [[ ! -d "${VENV}" ]]; then
  step "Creating virtualenv…"
  python3 -m venv "${VENV}"
fi
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${WEB}/requirements.txt"
ok "Python packages up to date."

# ── Docker services ───────────────────────────────────────────────────────────
hdr "Docker services"
cd "${SHERLOCK}"
step "Starting Ollama + ChromaDB…"
docker compose up -d

echo -n "  Waiting for Ollama"
for i in $(seq 1 30); do
  curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && echo "" && ok "Ollama is ready." && break
  echo -n "."; sleep 2
  [[ $i -eq 30 ]] && echo "" && warn "Ollama slow to respond — check: docker logs sherlock-ollama"
done

echo -n "  Waiting for ChromaDB"
for i in $(seq 1 20); do
  curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1 && echo "" && ok "ChromaDB is ready." && break
  echo -n "."; sleep 2
  [[ $i -eq 20 ]] && echo "" && warn "ChromaDB slow — check: docker logs sherlock-chroma"
done

# ── Ollama models ─────────────────────────────────────────────────────────────
hdr "Ollama models"

pull_if_missing() {
  local model="$1"
  curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q "\"${model}\"" \
    && ok "${model}: already present" \
    || { step "Pulling ${model}…"; docker exec sherlock-ollama ollama pull "${model}" \
         && ok "${model}: pulled" || warn "${model}: pull failed"; }
}

pull_if_missing "mxbai-embed-large"

if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q "sherlock-rag"; then
  ok "sherlock-rag: already present"
elif [[ "${LLM_MODEL}" == "sherlock-rag" ]] && [[ -f "${SHERLOCK}/Modelfile" ]]; then
  step "Creating sherlock-rag model…"
  docker exec -i sherlock-ollama ollama create sherlock-rag -f - < "${SHERLOCK}/Modelfile" \
    && ok "sherlock-rag: created" || warn "sherlock-rag: creation failed"
else
  pull_if_missing "${LLM_MODEL}"
fi

# ── Whisper ───────────────────────────────────────────────────────────────────
hdr "Whisper model"
WHISPER_DIR="${SHERLOCK}/models/whisper"
if ls "${WHISPER_DIR}"/models--* &>/dev/null 2>&1; then
  ok "Whisper '${WHISPER_MODEL}' already present."
else
  step "Downloading Whisper '${WHISPER_MODEL}'…"
  "${VENV}/bin/python" -c "
from faster_whisper import WhisperModel; import sys
WhisperModel(sys.argv[1], device='cpu', download_root=sys.argv[2])
" "${WHISPER_MODEL}" "${WHISPER_DIR}" && ok "Whisper downloaded." \
    || warn "Whisper download failed — audio transcription disabled until fixed."
fi

# ── systemd user services ─────────────────────────────────────────────────────
hdr "systemd: sherlock-web"
mkdir -p "${SYSTEMD_DIR}"

cat > "${SYSTEMD_DIR}/sherlock-web.service" << EOF
[Unit]
Description=Sherlock Web Application
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
WorkingDirectory=${WEB}
ExecStart=${VENV}/bin/python ${WEB}/main.py
Restart=always
RestartSec=5
StandardOutput=append:${SHERLOCK}/logs/sherlock-web.log
StandardError=append:${SHERLOCK}/logs/sherlock-web.log
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

cat > "${SYSTEMD_DIR}/sherlock-indexer.service" << EOF
[Unit]
Description=Sherlock NAS Indexer
After=network.target sherlock-web.service

[Service]
Type=oneshot
WorkingDirectory=${WEB}
ExecStart=${VENV}/bin/python ${WEB}/run_indexer.py
StandardOutput=append:${SHERLOCK}/logs/sherlock-indexer.log
StandardError=append:${SHERLOCK}/logs/sherlock-indexer.log
EOF

cat > "${SYSTEMD_DIR}/sherlock-indexer.timer" << EOF
[Unit]
Description=Run Sherlock indexer every 30 minutes
Requires=sherlock-indexer.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Unit=sherlock-indexer.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable sherlock-web.service
systemctl --user enable sherlock-indexer.timer
systemctl --user restart sherlock-web.service
systemctl --user start sherlock-indexer.timer
loginctl enable-linger "${USER}"
ok "sherlock-web service enabled (auto-starts on boot)."
ok "sherlock-indexer timer enabled (runs every 30 min)."

# ── DB init + admin ───────────────────────────────────────────────────────────
hdr "Database"
cd "${WEB}"
"${VENV}/bin/python" -c "from models import init_db; init_db(); print('  DB initialized.')"
ok "sherlock.db ready."

hdr "Admin account"
ADMIN_EXISTS=$("${VENV}/bin/python" -c "
from models import SessionLocal, init_db
from auth import ensure_admin_exists
init_db(); db = SessionLocal()
print('yes' if ensure_admin_exists(db) else 'no'); db.close()
" 2>/dev/null || echo "no")

if [[ "${ADMIN_EXISTS}" == "yes" ]]; then
  ok "Admin account already exists."
else
  "${VENV}/bin/python" "${WEB}/create_admin.py"
fi

# ── Health check ──────────────────────────────────────────────────────────────
hdr "Health check"
echo -n "  Waiting for sherlock-web"
for i in $(seq 1 20); do
  curl -sf http://localhost:3000 >/dev/null 2>&1 && echo "" && ok "sherlock-web is up at http://localhost:3000" && break
  echo -n "."; sleep 1
  [[ $i -eq 20 ]] && echo "" && warn "Web not yet responding — check: journalctl --user -u sherlock-web -f"
done

# ── Mark setup done ───────────────────────────────────────────────────────────
touch ~/.sherlock_setup_done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  ══════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Sherlock is ready.${RESET}"
echo -e "${BOLD}${GREEN}  ══════════════════════════════════════════${RESET}"
echo ""
echo "  URL:     http://localhost:3000"
echo "  Tier:    ${TIER} — model: ${LLM_MODEL}"
echo "  Status:  ./setup-linux.sh --status"
echo "  Logs:    ${SHERLOCK}/logs/"
echo ""
echo "  From other office computers:"
echo "  → http://$(hostname -I | awk '{print $1}'):3000"
echo ""
