#!/bin/bash
# Sherlock — Update script
# Pulls latest code from GitHub and restarts services
set -euo pipefail

SHERLOCK_DIR="${HOME}/Sherlock"
VENV="${SHERLOCK_DIR}/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
step() { echo -e "  ${BLUE}→${RESET}  $*"; }

echo -e "\n${BOLD}Sherlock Update${RESET}\n"

step "Pulling latest code..."
git -C "${SHERLOCK_DIR}" pull --ff-only
ok "Code updated"

step "Updating Python dependencies..."
"${VENV}/bin/pip" install -q -r "${SHERLOCK_DIR}/requirements.txt"
ok "Dependencies updated"

step "Restarting Sherlock..."
sudo systemctl restart sherlock-web.service
ok "Sherlock restarted"

echo ""
ok "Update complete"
