#!/bin/bash
# nginx/install.sh — Install and configure Nginx for Sherlock
# Idempotent: safe to re-run.
#
# Usage:
#   ~/Sherlock/nginx/install.sh
#   ~/Sherlock/nginx/install.sh --hostname sherlock.lawfirm.local

set -euo pipefail

SHERLOCK="${HOME}/Sherlock"
NGINX_CONF="${SHERLOCK}/nginx/sherlock.conf"
USER=$(whoami)
HOSTNAME="${1:-}"
[[ "${HOSTNAME}" == "--hostname" ]] && HOSTNAME="${2:-sherlock.local}" || HOSTNAME="${HOSTNAME:-sherlock.local}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*"; exit 1; }
hdr()  { echo -e "\n${BOLD}${BLUE}$*${RESET}"; }

hdr "Sherlock Nginx Setup"

# ── 1. Install Nginx ──────────────────────────────────────────────────────────
hdr "1. Installing Nginx"
if command -v nginx &>/dev/null; then
  ok "Nginx already installed: $(nginx -v 2>&1)"
else
  echo "  Installing via Homebrew..."
  brew install nginx
  ok "Nginx installed"
fi

# Detect brew prefix (Apple Silicon vs Intel)
BREW_PREFIX=$(brew --prefix)
NGINX_SERVERS_DIR="${BREW_PREFIX}/etc/nginx/servers"
mkdir -p "${NGINX_SERVERS_DIR}"

# ── 2. Generate TLS cert ──────────────────────────────────────────────────────
hdr "2. TLS Certificate"
if [[ -f "${SHERLOCK}/nginx/certs/sherlock.crt" ]]; then
  ok "Certificate already exists — skipping (delete certs/ to regenerate)"
else
  bash "${SHERLOCK}/nginx/gen-cert.sh" --hostname "${HOSTNAME}"
fi

# ── 3. Install Nginx config ───────────────────────────────────────────────────
hdr "3. Nginx Configuration"

# Substitute the real username into the config
INSTALLED_CONF="${NGINX_SERVERS_DIR}/sherlock.conf"
sed "s|SHERLOCK_USER|${USER}|g" "${NGINX_CONF}" > "${INSTALLED_CONF}"
ok "Config installed → ${INSTALLED_CONF}"

# Validate
nginx -t 2>&1 | grep -q "syntax is ok" && ok "Config syntax valid" || {
  err "Nginx config test failed — check ${INSTALLED_CONF}"
}

# ── 4. launchd plist ─────────────────────────────────────────────────────────
hdr "4. launchd Agent"
PLIST="${HOME}/Library/LaunchAgents/com.sherlock.nginx.plist"
cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>com.sherlock.nginx</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BREW_PREFIX}/bin/nginx</string>
    <string>-g</string>
    <string>daemon off;</string>
  </array>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>StandardOutPath</key>    <string>${SHERLOCK}/logs/nginx-launchd.log</string>
  <key>StandardErrorPath</key>  <string>${SHERLOCK}/logs/nginx-launchd.log</string>
</dict>
</plist>
EOF

# Unload old version if running
launchctl unload "${PLIST}" 2>/dev/null || true
launchctl load "${PLIST}"
ok "launchd agent loaded: com.sherlock.nginx"

# ── 5. Trust cert on this Mac ─────────────────────────────────────────────────
hdr "5. Trust Certificate (this machine)"
CERT="${SHERLOCK}/nginx/certs/sherlock.crt"
if sudo security find-certificate -c "Sherlock" /Library/Keychains/System.keychain &>/dev/null; then
  ok "Certificate already trusted in System keychain"
else
  echo "  Adding to macOS trusted root store (requires sudo)..."
  sudo security add-trusted-cert -d -r trustRoot \
    -k /Library/Keychains/System.keychain "${CERT}" \
    && ok "Certificate trusted" \
    || warn "Could not add to keychain — users will see browser warning"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}Nginx ready.${RESET}"
echo ""

# Detect LAN IP
LAN_IP=$(ifconfig | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | head -1)
echo "  Sherlock is now available at:"
echo "    https://localhost"
[[ -n "${LAN_IP}" ]] && echo "    https://${LAN_IP}  (LAN)"
echo "    https://${HOSTNAME}  (if DNS configured)"
echo ""
echo "  FastAPI still accessible directly at: http://localhost:3000"
echo "  (restrict this with a firewall rule in production)"
echo ""
echo "  To distribute the cert to other LAN machines:"
echo "    ${CERT}"
