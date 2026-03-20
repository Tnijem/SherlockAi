#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Sherlock AI — NAS Setup Wizard
# Walks through mounting a NAS share and linking it to Sherlock
# ═══════════════════════════════════════════════════════════════════════════════

SHERLOCK_DIR="${HOME}/Sherlock"
CONF="${SHERLOCK_DIR}/sherlock.conf"
MOUNTS_DIR="/mnt/sherlock-nas"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn()   { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
step()   { echo -e "  ${BLUE}→${RESET}  $*"; }
info()   { echo -e "  ${CYAN}ℹ${RESET}  $*"; }
fail()   { echo -e "\n  ${RED}✗  $*${RESET}\n"; exit 1; }
ask()    { echo -e "\n  ${BOLD}$*${RESET}"; }
divider(){ echo -e "\n  ${BLUE}────────────────────────────────────────────${RESET}"; }

clear
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗"
echo -e "║     SHERLOCK AI — NAS Setup Wizard           ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  This wizard will connect a network share (NAS)"
echo -e "  to Sherlock so it can search and index your files."
echo ""

[[ ! -f "${CONF}" ]] && fail "sherlock.conf not found at ${CONF}. Is Sherlock installed?"
sudo -v || fail "Need sudo access to mount network shares."

# ── Install cifs-utils if needed ──────────────────────────────────────────────
if ! dpkg -l cifs-utils &>/dev/null 2>&1; then
  step "Installing cifs-utils (required for SMB mounts)..."
  sudo apt-get install -y -qq cifs-utils nfs-common 2>/dev/null \
    && ok "cifs-utils installed" \
    || warn "Could not install cifs-utils — SMB mounts may fail"
fi

# ── Show existing NAS mounts ──────────────────────────────────────────────────
EXISTING=$(grep "^NAS_PATHS=" "${CONF}" | cut -d= -f2-)
if [[ -n "${EXISTING}" ]]; then
  divider
  echo -e "\n  ${BOLD}Current NAS paths in Sherlock:${RESET}"
  IFS=',' read -ra PATHS <<< "${EXISTING}"
  for p in "${PATHS[@]}"; do
    [[ -n "$p" ]] && echo -e "    ${CYAN}•${RESET} $p"
  done
fi

divider

# ── Step 1: NAS protocol ──────────────────────────────────────────────────────
ask "Step 1 of 6 — What type of NAS share?"
echo ""
echo -e "    ${BOLD}1)${RESET} SMB / CIFS  ${CYAN}(Windows, Synology, QNAP, most NAS devices)${RESET}"
echo -e "    ${BOLD}2)${RESET} NFS         ${CYAN}(Linux-based NAS, TrueNAS, some Synology)${RESET}"
echo ""
read -rp "  Choice [1]: " PROTO_CHOICE
PROTO_CHOICE="${PROTO_CHOICE:-1}"

case "${PROTO_CHOICE}" in
  1) PROTO="smb" ;;
  2) PROTO="nfs" ;;
  *) fail "Invalid choice" ;;
esac
ok "Protocol: ${PROTO^^}"

# ── Step 2: NAS IP or hostname ────────────────────────────────────────────────
divider
ask "Step 2 of 6 — NAS IP address or hostname"
info "e.g.  192.168.4.10   or   nas.local   or   synology.home"
echo ""
read -rp "  NAS address: " NAS_HOST
[[ -z "${NAS_HOST}" ]] && fail "NAS address is required."

# Test reachability
step "Testing connection to ${NAS_HOST}..."
if ping -c 2 -W 3 "${NAS_HOST}" &>/dev/null; then
  ok "${NAS_HOST} is reachable"
else
  warn "${NAS_HOST} did not respond to ping — continuing anyway (firewall may block ICMP)"
fi

# ── Step 3: Share name ────────────────────────────────────────────────────────
divider
ask "Step 3 of 6 — Share name"

if [[ "${PROTO}" == "smb" ]]; then
  info "The folder name shared on your NAS"
  info "e.g.  LegalFiles   or   ClientDocs   or   shared"
  echo ""
  read -rp "  Share name: " SHARE_NAME
  [[ -z "${SHARE_NAME}" ]] && fail "Share name is required."
  SHARE_PATH="//${NAS_HOST}/${SHARE_NAME}"
else
  info "The export path on your NAS"
  info "e.g.  /volume1/LegalFiles   or   /export/docs"
  echo ""
  read -rp "  Export path: " SHARE_NAME
  [[ -z "${SHARE_NAME}" ]] && fail "Export path is required."
  SHARE_PATH="${NAS_HOST}:${SHARE_NAME}"
fi
ok "Share: ${SHARE_PATH}"

# ── Step 4: Credentials (SMB only) ───────────────────────────────────────────
if [[ "${PROTO}" == "smb" ]]; then
  divider
  ask "Step 4 of 6 — Credentials"
  echo ""
  echo -e "    ${BOLD}1)${RESET} Username & password  ${CYAN}(most NAS devices)${RESET}"
  echo -e "    ${BOLD}2)${RESET} No password (guest/anonymous access)"
  echo ""
  read -rp "  Choice [1]: " CRED_CHOICE
  CRED_CHOICE="${CRED_CHOICE:-1}"

  if [[ "${CRED_CHOICE}" == "1" ]]; then
    echo ""
    read -rp "  Username: " NAS_USER
    read -rsp "  Password: " NAS_PASS
    echo ""
    [[ -z "${NAS_USER}" ]] && fail "Username required."

    # Store credentials in a protected file
    CREDS_FILE="/etc/sherlock/nas-${NAS_HOST//[^a-zA-Z0-9]/-}.creds"
    sudo mkdir -p /etc/sherlock
    sudo tee "${CREDS_FILE}" > /dev/null << CREDSEOF
username=${NAS_USER}
password=${NAS_PASS}
CREDSEOF
    sudo chmod 600 "${CREDS_FILE}"
    ok "Credentials saved to ${CREDS_FILE}"
    MOUNT_OPTS="credentials=${CREDS_FILE},uid=$(id -u),gid=$(id -g),iocharset=utf8,vers=3.0"
  else
    MOUNT_OPTS="guest,uid=$(id -u),gid=$(id -g),iocharset=utf8,vers=3.0"
    CREDS_FILE=""
  fi
else
  divider
  echo -e "\n  ${BOLD}Step 4 of 6${RESET} — Skipped (NFS doesn't use passwords)"
  MOUNT_OPTS="defaults,_netdev,soft,timeo=30"
  CREDS_FILE=""
fi

# ── Step 5: Local mount point ─────────────────────────────────────────────────
divider
ask "Step 5 of 6 — Local folder name for this share"
info "Files will be accessible at /mnt/sherlock-nas/<name>"
info "e.g.  legal-files   client-docs   archive"
echo ""

# Suggest a name from the share
SUGGESTED=$(echo "${SHARE_NAME}" | tr '/' '-' | tr ' ' '-' | tr '[:upper:]' '[:lower:]' | sed 's/^-//')
read -rp "  Folder name [${SUGGESTED}]: " MOUNT_NAME
MOUNT_NAME="${MOUNT_NAME:-${SUGGESTED}}"
MOUNT_POINT="${MOUNTS_DIR}/${MOUNT_NAME}"

ok "Will mount at: ${MOUNT_POINT}"

# ── Step 6: Confirm ───────────────────────────────────────────────────────────
divider
echo ""
echo -e "  ${BOLD}Summary — review before applying:${RESET}"
echo ""
echo -e "    Share:       ${CYAN}${SHARE_PATH}${RESET}"
echo -e "    Mount point: ${CYAN}${MOUNT_POINT}${RESET}"
echo -e "    Protocol:    ${CYAN}${PROTO^^}${RESET}"
[[ -n "${CREDS_FILE}" ]] && echo -e "    Credentials: ${CYAN}${CREDS_FILE}${RESET}"
echo ""
read -rp "  Apply and mount? [Y/n]: " CONFIRM
CONFIRM="${CONFIRM:-Y}"
[[ "${CONFIRM}" =~ ^[Nn] ]] && echo "  Cancelled." && exit 0

# ── Mount ─────────────────────────────────────────────────────────────────────
divider
echo ""
step "Creating mount point..."
sudo mkdir -p "${MOUNT_POINT}"

step "Mounting ${SHARE_PATH}..."
if [[ "${PROTO}" == "smb" ]]; then
  sudo mount -t cifs "${SHARE_PATH}" "${MOUNT_POINT}" -o "${MOUNT_OPTS}" 2>/tmp/nas-mount-err
else
  sudo mount -t nfs "${SHARE_PATH}" "${MOUNT_POINT}" -o "${MOUNT_OPTS}" 2>/tmp/nas-mount-err
fi

if mountpoint -q "${MOUNT_POINT}"; then
  ok "Mounted successfully!"
  FILE_COUNT=$(find "${MOUNT_POINT}" -maxdepth 2 -type f 2>/dev/null | wc -l)
  ok "Files visible: ~${FILE_COUNT} (top 2 levels)"
else
  echo -e "\n  ${RED}✗  Mount failed. Error:${RESET}"
  cat /tmp/nas-mount-err
  echo ""
  echo -e "  ${YELLOW}Common fixes:${RESET}"
  echo -e "    • Wrong share name — check your NAS admin panel for exact share name"
  echo -e "    • Wrong credentials — verify username/password on NAS"
  echo -e "    • SMB version — try adding  ,vers=2.0  or  ,vers=1.0  to mount options"
  echo -e "    • Firewall on NAS — ensure SMB ports 445/139 are open"
  exit 1
fi

# ── Persist in /etc/fstab ─────────────────────────────────────────────────────
step "Adding to /etc/fstab for auto-mount on reboot..."

# Remove old entry for this mount point if exists
sudo sed -i "\|${MOUNT_POINT}|d" /etc/fstab

if [[ "${PROTO}" == "smb" ]]; then
  FSTAB_ENTRY="${SHARE_PATH}  ${MOUNT_POINT}  cifs  ${MOUNT_OPTS},_netdev,nofail  0  0"
else
  FSTAB_ENTRY="${SHARE_PATH}  ${MOUNT_POINT}  nfs  ${MOUNT_OPTS},nofail  0  0"
fi

echo "${FSTAB_ENTRY}" | sudo tee -a /etc/fstab > /dev/null
ok "fstab updated (auto-mounts on boot)"

# ── Update sherlock.conf ──────────────────────────────────────────────────────
step "Adding to Sherlock NAS_PATHS..."

CURRENT_PATHS=$(grep "^NAS_PATHS=" "${CONF}" | cut -d= -f2-)

if [[ -z "${CURRENT_PATHS}" ]]; then
  NEW_PATHS="${MOUNT_POINT}"
else
  # Avoid duplicates
  if echo "${CURRENT_PATHS}" | grep -q "${MOUNT_POINT}"; then
    warn "Path already in sherlock.conf — skipping"
    NEW_PATHS="${CURRENT_PATHS}"
  else
    NEW_PATHS="${CURRENT_PATHS},${MOUNT_POINT}"
  fi
fi

# Update sherlock.conf
sed -i "s|^NAS_PATHS=.*|NAS_PATHS=${NEW_PATHS}|" "${CONF}"
ok "sherlock.conf updated"

# ── Trigger Sherlock re-index ─────────────────────────────────────────────────
step "Signalling Sherlock to pick up new NAS path..."
if curl -sf http://localhost:8000/api/health &>/dev/null; then
  ok "Sherlock is running — new NAS path will be available immediately"
  info "Go to Sherlock → Upload → NAS to index files from this share"
else
  warn "Sherlock not currently running — NAS path will load on next start"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
echo -e "║     NAS Share Connected!                     ║"
echo -e "╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Share:${RESET}       ${SHARE_PATH}"
echo -e "  ${BOLD}Local path:${RESET}  ${MOUNT_POINT}"
echo -e "  ${BOLD}In Sherlock:${RESET} NAS_PATHS includes this folder"
echo ""
echo -e "  ${YELLOW}To add another NAS share, run this script again.${RESET}"
echo -e "  ${YELLOW}To remove a share, edit /etc/fstab and sherlock.conf${RESET}"
echo ""
