#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Sherlock AI — macOS Installer
# Supports: macOS 13 Ventura+ | Apple Silicon & Intel | Fresh install
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SHERLOCK_DIR="${HOME}/Sherlock"
VENV="${SHERLOCK_DIR}/venv"
LOG_DIR="${SHERLOCK_DIR}/logs"
LOG="${LOG_DIR}/install-$(date +%Y%m%d-%H%M%S).log"
VERSION_FILE="${SHERLOCK_DIR}/VERSION"
PLIST_LABEL="com.sherlock.ai"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_LABEL}.plist"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET}  $*" | tee -a "${LOG}"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*" | tee -a "${LOG}"; }
step() { echo -e "  ${BLUE}→${RESET}  $*" | tee -a "${LOG}"; }
hdr()  { echo -e "\n${BOLD}${BLUE}══  $*  ══${RESET}" | tee -a "${LOG}"; }
fail() { echo -e "\n  ${RED}✗  ERROR: $*${RESET}\n"; exit 1; }

# ── Detect script location (USB or local) ─────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)" 2>/dev/null || SCRIPT_DIR="$(pwd)"
# Walk up to find sherlock-bundle or use script dir as bundle root
if [[ -f "${SCRIPT_DIR}/sherlock-source.tar.gz" ]]; then
  BUNDLE_DIR="${SCRIPT_DIR}"
elif [[ -f "${SCRIPT_DIR}/../sherlock-source.tar.gz" ]]; then
  BUNDLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  BUNDLE_DIR=""
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗"
echo -e "║     SHERLOCK AI — macOS Installer            ║"
echo -e "║     v$(cat "${SCRIPT_DIR}/../VERSION" 2>/dev/null || echo '?')                                    ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""

[[ "${EUID}" -eq 0 ]] && fail "Do not run as root. Run as your regular user."

# ── Detect hardware ───────────────────────────────────────────────────────────
ARCH=$(uname -m)
RAM_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
MACOS_VER=$(sw_vers -productVersion)

echo -e "  macOS ${MACOS_VER} | ${ARCH} | ${RAM_GB}GB RAM"
echo ""

# Pick Ollama model based on RAM
if   [[ "${RAM_GB}" -ge 64 ]]; then OLLAMA_LLM_MODEL="gemma3:27b"
elif [[ "${RAM_GB}" -ge 24 ]]; then OLLAMA_LLM_MODEL="gemma3:12b"
elif [[ "${RAM_GB}" -ge 12 ]]; then OLLAMA_LLM_MODEL="gemma3:4b"
else                                 OLLAMA_LLM_MODEL="gemma3:1b"; fi
OLLAMA_EMBED_MODEL="mxbai-embed-large"

echo -e "  ${BLUE}Model selected:${RESET} ${OLLAMA_LLM_MODEL} (${RAM_GB}GB RAM)"
echo ""

mkdir -p "${LOG_DIR}"
echo "Sherlock macOS Install — $(date)" > "${LOG}"

# ── Step 1: Xcode Command Line Tools ─────────────────────────────────────────
hdr "Step 1: Xcode Command Line Tools"
if xcode-select -p &>/dev/null; then
  ok "Already installed ($(xcode-select -p))"
else
  step "Installing Xcode CLI tools..."
  xcode-select --install 2>>"${LOG}" || true
  echo -e "  ${YELLOW}⚠  A dialog appeared — click Install, then re-run this script.${RESET}"
  exit 0
fi

# ── Step 2: Homebrew ──────────────────────────────────────────────────────────
hdr "Step 2: Homebrew"
if command -v brew &>/dev/null; then
  ok "Homebrew already installed ($(brew --version | head -1))"
else
  step "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" >> "${LOG}" 2>&1

  # Add brew to PATH for Apple Silicon
  if [[ "${ARCH}" == "arm64" ]]; then
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "${HOME}/.zprofile"
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
  ok "Homebrew installed"
fi

# Silence brew hints
export HOMEBREW_NO_ENV_HINTS=1
export HOMEBREW_NO_AUTO_UPDATE=1

# ── Step 3: System packages via Homebrew ─────────────────────────────────────
hdr "Step 3: System Packages"

BREW_PKGS=(python@3.11 tesseract ffmpeg jq)

for pkg in "${BREW_PKGS[@]}"; do
  if brew list "${pkg}" &>/dev/null; then
    ok "${pkg} already installed"
  else
    step "Installing ${pkg}..."
    brew install "${pkg}" >> "${LOG}" 2>&1 && ok "${pkg}" || warn "${pkg} failed — continuing"
  fi
done

# LibreOffice (cask)
if brew list --cask libreoffice &>/dev/null || [[ -d "/Applications/LibreOffice.app" ]]; then
  ok "LibreOffice already installed"
else
  step "Installing LibreOffice (large download ~350MB)..."
  brew install --cask libreoffice >> "${LOG}" 2>&1 && ok "LibreOffice" || warn "LibreOffice failed — document conversion may be limited"
fi

# ── Step 4: Ollama ────────────────────────────────────────────────────────────
hdr "Step 4: Ollama"
if command -v ollama &>/dev/null; then
  ok "Ollama already installed ($(ollama --version 2>/dev/null || echo 'installed'))"
else
  step "Installing Ollama..."
  if [[ -n "${BUNDLE_DIR}" ]] && [[ -f "${BUNDLE_DIR}/ollama/ollama-darwin.tar.gz" ]]; then
    # Offline install from USB bundle
    step "Installing from bundle (offline)..."
    sudo tar -xzf "${BUNDLE_DIR}/ollama/ollama-darwin.tar.gz" -C /usr/local/bin/ >> "${LOG}" 2>&1
  else
    brew install ollama >> "${LOG}" 2>&1 || \
      curl -fsSL https://ollama.com/install.sh | sh >> "${LOG}" 2>&1
  fi
  ok "Ollama installed"
fi

# Start Ollama service
if ! pgrep -x ollama &>/dev/null; then
  step "Starting Ollama service..."
  ollama serve >> "${LOG}" 2>&1 &
  sleep 3
  ok "Ollama service started"
else
  ok "Ollama already running"
fi

# Pull models
hdr "Step 5: Ollama Models"
for model in "${OLLAMA_LLM_MODEL}" "${OLLAMA_EMBED_MODEL}"; do
  if ollama list 2>/dev/null | grep -q "${model%:*}"; then
    ok "${model} already present"
  elif [[ -n "${BUNDLE_DIR}" ]] && [[ -d "${BUNDLE_DIR}/ollama/models" ]]; then
    step "Loading ${model} from bundle..."
    OLLAMA_MODELS="${BUNDLE_DIR}/ollama/models" ollama pull "${model}" >> "${LOG}" 2>&1 && ok "${model}" || warn "${model} failed"
  else
    step "Downloading ${model} (this may take a while)..."
    ollama pull "${model}" >> "${LOG}" 2>&1 && ok "${model}" || warn "${model} failed"
  fi
done

# ── Step 6: Sherlock source ───────────────────────────────────────────────────
hdr "Step 6: Sherlock Source"
mkdir -p "${SHERLOCK_DIR}"

if [[ -n "${BUNDLE_DIR}" ]] && [[ -f "${BUNDLE_DIR}/sherlock-source.tar.gz" ]]; then
  step "Extracting from bundle..."
  tar -xzf "${BUNDLE_DIR}/sherlock-source.tar.gz" -C "${SHERLOCK_DIR}" >> "${LOG}" 2>&1
  ok "Sherlock source extracted"
elif [[ -f "${SCRIPT_DIR}/sherlock-source.tar.gz" ]]; then
  step "Extracting from local bundle..."
  tar -xzf "${SCRIPT_DIR}/sherlock-source.tar.gz" -C "${SHERLOCK_DIR}" >> "${LOG}" 2>&1
  ok "Sherlock source extracted"
else
  warn "No sherlock-source.tar.gz found — skipping source extract (existing install preserved)"
fi

# ── Step 7: Python venv + pip packages ───────────────────────────────────────
hdr "Step 7: Python Environment"
PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"
[[ ! -f "${PYTHON_BIN}" ]] && PYTHON_BIN="python3"

if [[ ! -d "${VENV}" ]]; then
  step "Creating Python virtual environment..."
  "${PYTHON_BIN}" -m venv "${VENV}" >> "${LOG}" 2>&1
  ok "Virtual environment created"
else
  ok "Virtual environment already exists"
fi

step "Installing Python packages..."
if [[ -n "${BUNDLE_DIR}" ]] && [[ -d "${BUNDLE_DIR}/pip-wheels" ]]; then
  # Offline install from bundled wheels
  "${VENV}/bin/pip" install --no-index --find-links="${BUNDLE_DIR}/pip-wheels" \
    -r "${SHERLOCK_DIR}/requirements.txt" >> "${LOG}" 2>&1 \
    && ok "Python packages installed (offline)" \
    || {
      warn "Offline install incomplete — trying online fallback..."
      "${VENV}/bin/pip" install -r "${SHERLOCK_DIR}/requirements.txt" >> "${LOG}" 2>&1 && ok "Python packages installed (online)"
    }
else
  "${VENV}/bin/pip" install --upgrade pip >> "${LOG}" 2>&1
  "${VENV}/bin/pip" install -r "${SHERLOCK_DIR}/requirements.txt" >> "${LOG}" 2>&1 && ok "Python packages installed"
fi

# ── Step 8: sherlock.conf ─────────────────────────────────────────────────────
hdr "Step 8: Configuration"
CONF="${SHERLOCK_DIR}/sherlock.conf"
if [[ ! -f "${CONF}" ]]; then
  LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")
  cat > "${CONF}" << EOF
# Sherlock AI — Configuration
OLLAMA_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=${OLLAMA_LLM_MODEL}
EMBED_MODEL=${OLLAMA_EMBED_MODEL}
CHROMA_PATH=${SHERLOCK_DIR}/chroma
UPLOAD_DIR=${SHERLOCK_DIR}/uploads
SECRET_KEY=$(openssl rand -hex 32)
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO
EOF
  ok "sherlock.conf created"
else
  ok "sherlock.conf already exists (preserved)"
fi

# ── Step 9: Directories ───────────────────────────────────────────────────────
hdr "Step 9: Data Directories"
for dir in uploads chroma logs outputs; do
  mkdir -p "${SHERLOCK_DIR}/${dir}"
done
ok "Data directories ready"

# ── Step 10: LaunchAgent (auto-start on login) ────────────────────────────────
hdr "Step 10: Auto-Start Service (LaunchAgent)"
mkdir -p "${HOME}/Library/LaunchAgents"

cat > "${PLIST_PATH}" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${VENV}/bin/python</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>main:app</string>
    <string>--host</string>
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${SHERLOCK_DIR}/web</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/sherlock.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/sherlock.log</string>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
EOF

# Load it
launchctl unload "${PLIST_PATH}" 2>/dev/null || true
launchctl load "${PLIST_PATH}" 2>/dev/null && ok "LaunchAgent loaded — Sherlock will start on login" \
  || warn "LaunchAgent load failed — start manually with: ./restart.sh"

# ── Done ──────────────────────────────────────────────────────────────────────
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "localhost")

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
echo -e "║     SHERLOCK AI — Install Complete!          ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Open Sherlock:${RESET}  http://${LOCAL_IP}:8000"
echo -e "  ${BOLD}Log file:${RESET}       ${LOG}"
echo ""
echo -e "  ${BLUE}To start/stop manually:${RESET}"
echo -e "    launchctl start ${PLIST_LABEL}"
echo -e "    launchctl stop  ${PLIST_LABEL}"
echo ""
echo -e "  ${YELLOW}First login — create your admin account at the URL above.${RESET}"
echo ""
