#!/bin/bash
# Sherlock setup v2.0 — Production-ready installer
# Idempotent: safe to re-run. Skips steps already completed.
#
# Usage:
#   ./setup.sh            Full interactive setup (first run or reconfigure)
#   ./setup.sh --update   Re-install deps + restart services (skip prompts)
#   ./setup.sh --status   Show current service status

set -euo pipefail

VERSION="2.0"
SHERLOCK="${HOME}/Sherlock"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG="${SHERLOCK}/logs/setup-${TIMESTAMP}.log"
CONF="${SHERLOCK}/sherlock.conf"
VENV="${SHERLOCK}/venv"
WEB="${SHERLOCK}/web"
PLIST_WEB="${HOME}/Library/LaunchAgents/com.sherlock.web.plist"
PLIST_IDX="${HOME}/Library/LaunchAgents/com.sherlock.indexer.plist"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*"; }
hdr()  { echo -e "\n${BOLD}${BLUE}$*${RESET}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*"; }

# ── Args ──────────────────────────────────────────────────────────────────────
MODE="full"
[[ "${1:-}" == "--update" ]] && MODE="update"
[[ "${1:-}" == "--status" ]] && MODE="status"

# ── Logging ───────────────────────────────────────────────────────────────────
mkdir -p "${SHERLOCK}/logs"
exec > >(tee -a "${LOG}") 2>&1

# ── Status mode ───────────────────────────────────────────────────────────────
if [[ "${MODE}" == "status" ]]; then
  hdr "Sherlock Status"
  echo ""

  # Docker services
  for svc in sherlock-ollama sherlock-chroma; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${svc}$"; then
      ok "${svc}: running"
    else
      err "${svc}: stopped"
    fi
  done

  # Web app
  if curl -sf http://localhost:3000 >/dev/null 2>&1; then
    ok "sherlock-web: running (http://localhost:3000)"
  else
    err "sherlock-web: stopped"
  fi

  # Ollama / Chroma health
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    ok "Ollama API: healthy"
  else
    warn "Ollama API: not responding"
  fi
  if curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1; then
    ok "ChromaDB: healthy"
  else
    warn "ChromaDB: not responding"
  fi

  # DB & config
  [[ -f "${CONF}" ]] && ok "sherlock.conf: present" || warn "sherlock.conf: missing"
  [[ -f "${SHERLOCK}/data/sherlock.db" ]] && ok "sherlock.db: present" || warn "sherlock.db: missing"

  # Models
  if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q "mxbai-embed"; then
    ok "Embedding model: mxbai-embed-large"
  else
    warn "Embedding model: not yet pulled"
  fi

  # User count
  if [[ -f "${SHERLOCK}/data/sherlock.db" ]]; then
    USERS=$(cd "${WEB}" && "${VENV}/bin/python" create_admin.py list 2>/dev/null | grep -c "user\|admin" || echo "?")
    ok "Users in DB: ${USERS}"
  fi

  echo ""
  exit 0
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

# ── Dependency checks ─────────────────────────────────────────────────────────
hdr "Checking dependencies"
DEPS_OK=true

if command -v docker &>/dev/null; then
  ok "Docker: $(docker --version | awk '{print $3}' | tr -d ',')"
else
  err "Docker not found. Install Docker Desktop and re-run."
  DEPS_OK=false
fi

if command -v python3 &>/dev/null || [[ -f "${VENV}/bin/python" ]]; then
  PYVER=$("${VENV}/bin/python" --version 2>/dev/null || python3 --version)
  ok "Python: ${PYVER}"
else
  err "Python 3 not found."
  DEPS_OK=false
fi

if [[ "${DEPS_OK}" == "false" ]]; then
  err "Fix missing dependencies and re-run."
  exit 1
fi

# ── Interactive config (full mode only) ───────────────────────────────────────
if [[ "${MODE}" == "full" ]]; then
  hdr "Configuration"
  echo ""

  # Load existing values as defaults if re-running
  _conf_get() {
    [[ -f "${CONF}" ]] && grep "^${1}=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${2}"
  }

  read -p "  System name displayed in UI [$(_conf_get SYSTEM_NAME Sherlock)]: " INPUT_NAME
  SYSTEM_NAME="${INPUT_NAME:-$(_conf_get SYSTEM_NAME Sherlock)}"

  echo ""
  echo "  NAS mount path(s) — comma-separated if multiple."
  echo "  These must be readable SMB/NFS mounts on this machine."
  read -p "  NAS path(s) [$(_conf_get NAS_PATHS '')]: " INPUT_NAS
  NAS_PATHS="${INPUT_NAS:-$(_conf_get NAS_PATHS '')}"

  echo ""
  read -p "  Outputs directory [$(_conf_get OUTPUTS_DIR "${SHERLOCK}/outputs")]: " INPUT_OUT
  OUTPUTS_DIR="${INPUT_OUT:-$(_conf_get OUTPUTS_DIR "${SHERLOCK}/outputs")}"
  OUTPUTS_DIR="${OUTPUTS_DIR/#\~/${HOME}}"  # expand ~

  echo ""
  read -p "  Uploads directory [$(_conf_get UPLOADS_DIR "${SHERLOCK}/uploads")]: " INPUT_UPL
  UPLOADS_DIR="${INPUT_UPL:-$(_conf_get UPLOADS_DIR "${SHERLOCK}/uploads")}"
  UPLOADS_DIR="${UPLOADS_DIR/#\~/${HOME}}"

  echo ""
  read -p "  Ollama URL [$(_conf_get OLLAMA_URL http://localhost:11434)]: " INPUT_OLLAMA
  OLLAMA_URL="${INPUT_OLLAMA:-$(_conf_get OLLAMA_URL http://localhost:11434)}"

  echo ""
  read -p "  JWT session expiry in hours [$(_conf_get JWT_EXPIRY_HOURS 8)]: " INPUT_JWT
  JWT_EXPIRY_HOURS="${INPUT_JWT:-$(_conf_get JWT_EXPIRY_HOURS 8)}"

  echo ""
  read -p "  Whisper model size [tiny/base/small/medium/large] [$(_conf_get WHISPER_MODEL medium)]: " INPUT_WHISPER
  WHISPER_MODEL="${INPUT_WHISPER:-$(_conf_get WHISPER_MODEL medium)}"

  # Generate or preserve JWT secret
  EXISTING_SECRET=$(_conf_get JWT_SECRET "")
  if [[ -z "${EXISTING_SECRET}" ]]; then
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    ok "Generated new JWT secret."
  else
    JWT_SECRET="${EXISTING_SECRET}"
    ok "Preserving existing JWT secret."
  fi

  # Write sherlock.conf
  hdr "Writing sherlock.conf"
  cat > "${CONF}" << EOF
SYSTEM_NAME=${SYSTEM_NAME}
NAS_PATHS=${NAS_PATHS}
OUTPUTS_DIR=${OUTPUTS_DIR}
UPLOADS_DIR=${UPLOADS_DIR}
DB_PATH=${SHERLOCK}/data/sherlock.db
OLLAMA_URL=${OLLAMA_URL}
CHROMA_URL=http://localhost:8000
JWT_SECRET=${JWT_SECRET}
JWT_EXPIRY_HOURS=${JWT_EXPIRY_HOURS}
EMBED_MODEL=mxbai-embed-large
LLM_MODEL=sherlock-rag
WHISPER_MODEL=${WHISPER_MODEL}
WHISPER_MODEL_DIR=${SHERLOCK}/models/whisper
MAX_UPLOAD_MB=500
RAG_TOP_N=5
EOF
  ok "sherlock.conf written."
else
  # --update mode: load from existing conf
  SYSTEM_NAME=$(grep "^SYSTEM_NAME=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "Sherlock")
  OLLAMA_URL=$(grep "^OLLAMA_URL=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "http://localhost:11434")
  WHISPER_MODEL=$(grep "^WHISPER_MODEL=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "medium")
  OUTPUTS_DIR=$(grep "^OUTPUTS_DIR=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${SHERLOCK}/outputs")
  UPLOADS_DIR=$(grep "^UPLOADS_DIR=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "${SHERLOCK}/uploads")
fi

# ── Directory structure ───────────────────────────────────────────────────────
hdr "Creating directories"
for DIR in \
  "${SHERLOCK}/data" \
  "${SHERLOCK}/logs" \
  "${SHERLOCK}/models/whisper" \
  "${OUTPUTS_DIR}" \
  "${UPLOADS_DIR}" \
  "${WEB}/static/assets" \
  "${SHERLOCK}/samples/case1" \
  "${SHERLOCK}/samples/case2" \
  "${SHERLOCK}/samples/case3"
do
  mkdir -p "${DIR}"
  ok "${DIR}"
done

# Copy branding assets to web static dir
cp "${SHERLOCK}/branding/graphics/logo.svg"      "${WEB}/static/assets/logo.svg"      2>/dev/null || true
cp "${SHERLOCK}/branding/graphics/logo-mono.svg" "${WEB}/static/assets/logo-mono.svg" 2>/dev/null || true
cp "${SHERLOCK}/branding/graphics/app-icon.svg"  "${WEB}/static/assets/favicon.svg"   2>/dev/null || true
ok "Branding assets synced."

# ── Python dependencies ───────────────────────────────────────────────────────
hdr "Python dependencies"
if [[ ! -d "${VENV}" ]]; then
  step "Creating virtualenv…"
  python3 -m venv "${VENV}"
fi

step "Installing/upgrading packages…"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${WEB}/requirements.txt"
ok "Python packages up to date."

# Check for LibreOffice (optional — needed for .doc/.xls/.ppt)
if command -v libreoffice &>/dev/null || command -v soffice &>/dev/null; then
  ok "LibreOffice: available (legacy .doc/.xls/.ppt support enabled)"
else
  warn "LibreOffice not found — .doc, .xls, .ppt files will be skipped."
  warn "Install: brew install --cask libreoffice"
fi

# Check for Tesseract (OCR for images)
if command -v tesseract &>/dev/null; then
  ok "Tesseract OCR: $(tesseract --version 2>&1 | head -1)"
else
  warn "Tesseract not found — image OCR will be skipped."
  warn "Install: brew install tesseract"
fi

# ── Sample files ──────────────────────────────────────────────────────────────
hdr "Sample case files"
[[ ! -f "${SHERLOCK}/samples/case1/001-complaint.txt" ]] && \
  echo "Complaint — Smith v. Jones (2024). Plaintiff alleges breach of contract and seeks damages of \$2.4M." \
  > "${SHERLOCK}/samples/case1/001-complaint.txt"

[[ ! -f "${SHERLOCK}/samples/case2/002-motion.txt" ]] && \
  echo "Motion to Dismiss — Doe PI case. Grounds: lack of personal jurisdiction under FRCP 12(b)(2)." \
  > "${SHERLOCK}/samples/case2/002-motion.txt"

[[ ! -f "${SHERLOCK}/samples/case3/003-evidence.txt" ]] && \
  echo "Chain of custody log — Robbins case. Evidence item #14 transferred 2024-03-15." \
  > "${SHERLOCK}/samples/case3/003-evidence.txt"

ok "Sample files ready."

# ── Docker services ───────────────────────────────────────────────────────────
hdr "Docker services"
cd "${SHERLOCK}"

step "Starting Ollama + ChromaDB…"
docker compose up -d

# Wait for Ollama
echo -n "  Waiting for Ollama"
for i in $(seq 1 30); do
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo ""
    ok "Ollama is ready."
    break
  fi
  echo -n "."
  sleep 2
  if [[ $i -eq 30 ]]; then echo ""; warn "Ollama did not respond in 60s — check docker logs."; fi
done

# Wait for ChromaDB
echo -n "  Waiting for ChromaDB"
for i in $(seq 1 20); do
  if curl -sf http://localhost:8000/api/v2/heartbeat >/dev/null 2>&1; then
    echo ""
    ok "ChromaDB is ready."
    break
  fi
  echo -n "."
  sleep 2
  if [[ $i -eq 20 ]]; then echo ""; warn "ChromaDB did not respond in 40s — check docker logs."; fi
done

# ── Ollama models ─────────────────────────────────────────────────────────────
hdr "Ollama models"

pull_if_missing() {
  local model="$1"
  if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q "\"${model}\""; then
    ok "${model}: already present"
  else
    step "Pulling ${model}…"
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
      docker exec sherlock-ollama ollama pull "${model}" && ok "${model}: pulled" || warn "${model}: pull failed (air-gapped?)"
    else
      warn "Ollama not reachable — skipping pull. Pre-download ${model} before air-gap deployment."
    fi
  fi
}

pull_if_missing "mxbai-embed-large"

# Check for sherlock-rag modelfile
if curl -sf http://localhost:11434/api/tags 2>/dev/null | grep -q "sherlock-rag"; then
  ok "sherlock-rag: already present"
elif [[ -f "${SHERLOCK}/Modelfile" ]]; then
  step "Creating sherlock-rag model from Modelfile…"
  docker exec -i sherlock-ollama ollama create sherlock-rag -f - < "${SHERLOCK}/Modelfile" \
    && ok "sherlock-rag: created" \
    || warn "sherlock-rag: creation failed — check Modelfile"
else
  warn "Modelfile not found — sherlock-rag will fall back to llama3.1:8b"
  pull_if_missing "llama3.1:8b"
fi

# ── Whisper model ─────────────────────────────────────────────────────────────
hdr "Whisper model"
WHISPER_DIR="${SHERLOCK}/models/whisper"
if ls "${WHISPER_DIR}"/models--* &>/dev/null 2>&1; then
  ok "Whisper '${WHISPER_MODEL}' model files already present."
else
  step "Downloading Whisper '${WHISPER_MODEL}' model (requires internet)…"
  "${VENV}/bin/python" -c "
from faster_whisper import WhisperModel
import sys
print(f'  Downloading whisper/{sys.argv[1]} to {sys.argv[2]}...')
WhisperModel(sys.argv[1], device='cpu', download_root=sys.argv[2])
print('  Done.')
" "${WHISPER_MODEL}" "${WHISPER_DIR}" && ok "Whisper model downloaded." \
    || warn "Whisper download failed — audio transcription will not work until model is present."
fi

# ── launchd: Sherlock web app ─────────────────────────────────────────────────
hdr "launchd: sherlock-web"
cat > "${PLIST_WEB}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sherlock.web</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV}/bin/python</string>
        <string>${WEB}/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${WEB}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SHERLOCK}/logs/sherlock-web.log</string>
    <key>StandardErrorPath</key>
    <string>${SHERLOCK}/logs/sherlock-web.log</string>
    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
EOF

# Unload first if already loaded (idempotent)
launchctl unload "${PLIST_WEB}" 2>/dev/null || true
launchctl load "${PLIST_WEB}"
ok "sherlock-web launchd agent loaded (auto-starts on login)."

# ── launchd: NAS incremental indexer (runs every 30 min) ─────────────────────
hdr "launchd: sherlock-indexer"
cat > "${PLIST_IDX}" << EOF
<?xml version="1.0" encoding="UTF-8"">
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sherlock.indexer</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV}/bin/python</string>
        <string>${WEB}/run_indexer.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${WEB}</string>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SHERLOCK}/logs/sherlock-indexer.log</string>
    <key>StandardErrorPath</key>
    <string>${SHERLOCK}/logs/sherlock-indexer.log</string>
</dict>
</plist>
EOF

launchctl unload "${PLIST_IDX}" 2>/dev/null || true
launchctl load "${PLIST_IDX}"
ok "sherlock-indexer launchd agent loaded (runs every 30 min)."

# ── Initialize DB ─────────────────────────────────────────────────────────────
hdr "Database"
cd "${WEB}"
"${VENV}/bin/python" -c "from models import init_db; init_db(); print('  DB initialized.')"
ok "sherlock.db ready."

# ── Admin account ─────────────────────────────────────────────────────────────
hdr "Admin account"
ADMIN_EXISTS=$("${VENV}/bin/python" -c "
from models import SessionLocal, init_db
from auth import ensure_admin_exists
init_db()
db = SessionLocal()
print('yes' if ensure_admin_exists(db) else 'no')
db.close()
" 2>/dev/null || echo "no")

if [[ "${ADMIN_EXISTS}" == "yes" ]]; then
  ok "Admin account already exists."
  echo ""
  echo "  To manage users:"
  echo "    cd ${WEB} && ${VENV}/bin/python create_admin.py list"
else
  echo ""
  echo "  No admin account found. Create one now:"
  echo ""
  "${VENV}/bin/python" "${WEB}/create_admin.py"
fi

# ── Health check ──────────────────────────────────────────────────────────────
hdr "Health check"

# Wait up to 15s for web app to start
echo -n "  Waiting for sherlock-web to start"
for i in $(seq 1 15); do
  if curl -sf http://localhost:3000 >/dev/null 2>&1; then
    echo ""
    ok "sherlock-web is up at http://localhost:3000"
    break
  fi
  echo -n "."
  sleep 1
  if [[ $i -eq 15 ]]; then
    echo ""
    warn "Web app not yet responding — check logs: tail -f ${SHERLOCK}/logs/sherlock-web.log"
  fi
done

# ── Initial index (optional) ──────────────────────────────────────────────────
hdr "Initial NAS index"
NAS_PATHS_CONF=$(grep "^NAS_PATHS=" "${CONF}" 2>/dev/null | cut -d= -f2- || echo "")

if [[ -z "${NAS_PATHS_CONF}" ]]; then
  warn "No NAS paths configured — skipping initial index."
  warn "Add NAS_PATHS to sherlock.conf and trigger re-index from the Admin panel."
else
  echo ""
  echo "  NAS paths: ${NAS_PATHS_CONF}"
  read -p "  Run initial index now? (y/N): " DO_INDEX
  if [[ "${DO_INDEX}" =~ ^[Yy]$ ]]; then
    step "Starting indexer in background…"
    nohup "${VENV}/bin/python" "${WEB}/run_indexer.py" \
      > "${SHERLOCK}/logs/initial-index.log" 2>&1 &
    ok "Indexer started (PID $!). Monitor: tail -f ${SHERLOCK}/logs/initial-index.log"
  else
    step "Skipped — trigger from Admin panel or run: cd ${WEB} && ${VENV}/bin/python run_indexer.py"
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}  ══════════════════════════════════════════${RESET}"
echo -e "${BOLD}${GREEN}  Sherlock is ready.${RESET}"
echo -e "${BOLD}${GREEN}  ══════════════════════════════════════════${RESET}"
echo ""
echo "  URL:     http://localhost:3000"
echo "  Logs:    ${SHERLOCK}/logs/"
echo "  Config:  ${CONF}"
echo "  Status:  ./setup.sh --status"
echo ""
echo "  Log file: ${LOG}"
echo ""
