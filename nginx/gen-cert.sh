#!/bin/bash
# gen-cert.sh — Generate a self-signed TLS cert for Sherlock (air-gapped deployment)
# Re-running regenerates the cert. Valid for 10 years.
#
# Usage:
#   ~/Sherlock/nginx/gen-cert.sh
#   ~/Sherlock/nginx/gen-cert.sh --hostname sherlock.lawfirm.local

set -euo pipefail

SHERLOCK="${HOME}/Sherlock"
CERT_DIR="${SHERLOCK}/nginx/certs"
HOSTNAME="${1:-}"
[[ "${HOSTNAME}" == "--hostname" ]] && HOSTNAME="${2:-}" || HOSTNAME="${HOSTNAME:-sherlock.local}"

RED='\033[0;31m'; GREEN='\033[0;32m'; BOLD='\033[1m'; RESET='\033[0m'
ok()  { echo -e "  ${GREEN}✓${RESET}  $*"; }
err() { echo -e "  ${RED}✗${RESET}  $*"; exit 1; }

echo -e "\n${BOLD}Generating Sherlock TLS certificate${RESET}"
echo "  Hostname : ${HOSTNAME}"
echo "  Output   : ${CERT_DIR}/"
echo ""

mkdir -p "${CERT_DIR}"

# Detect LAN IPs to embed as SANs (so browser trusts the cert on LAN)
LAN_IPS=""
while IFS= read -r ip; do
  LAN_IPS+="IP:${ip},"
done < <(ifconfig 2>/dev/null | grep "inet " | awk '{print $2}' | grep -v "^127\.")
LAN_IPS="${LAN_IPS%,}"  # trim trailing comma

# Build SAN string
SAN="DNS:${HOSTNAME},DNS:localhost,IP:127.0.0.1"
[[ -n "${LAN_IPS}" ]] && SAN="${SAN},${LAN_IPS}"

echo "  SANs: ${SAN}"
echo ""

# Generate private key + self-signed cert
openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout "${CERT_DIR}/sherlock.key" \
  -out    "${CERT_DIR}/sherlock.crt" \
  -days   3650 \
  -subj   "/C=US/ST=State/L=City/O=Sherlock/OU=RAG/CN=${HOSTNAME}" \
  -addext "subjectAltName=${SAN}" \
  2>/dev/null

chmod 600 "${CERT_DIR}/sherlock.key"
chmod 644 "${CERT_DIR}/sherlock.crt"

ok "Certificate generated"
ok "Key:  ${CERT_DIR}/sherlock.key"
ok "Cert: ${CERT_DIR}/sherlock.crt"

echo ""
echo "  Valid for : 10 years"
echo "  Hostname  : ${HOSTNAME}"
echo "  SANs      : ${SAN}"
echo ""
echo "  To trust this cert on your Mac:"
echo "    sudo security add-trusted-cert -d -r trustRoot \\"
echo "      -k /Library/Keychains/System.keychain ${CERT_DIR}/sherlock.crt"
echo ""
echo "  To distribute to other machines on the LAN, copy sherlock.crt"
echo "  and add it to each machine's trusted root store."
