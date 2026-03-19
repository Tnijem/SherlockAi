#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Sherlock Demo Environment Loader
#
# Usage:
#   ./demo/demo-loader.sh load     # Install demo data + config
#   ./demo/demo-loader.sh unload   # Remove demo data, restore config
#   ./demo/demo-loader.sh status   # Check if demo is loaded
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SHERLOCK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEMO_DIR="$SHERLOCK_DIR/demo"
DEMO_DATA="$DEMO_DIR/case-martinez $DEMO_DIR/case-techvault $DEMO_DIR/case-greenfield"
CONF="$SHERLOCK_DIR/sherlock.conf"
CONF_BACKUP="$SHERLOCK_DIR/sherlock.conf.pre-demo"
DB="$SHERLOCK_DIR/data/sherlock.db"
DEMO_MARKER="$SHERLOCK_DIR/data/.demo-loaded"
API="http://localhost:3000"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

# ─────────────────────────────────────────────────────────────
# Check web app is running
# ─────────────────────────────────────────────────────────────
check_api() {
    if ! curl -s -o /dev/null -w '' "$API/api/auth/login" 2>/dev/null; then
        fail "Sherlock web app not running on $API"
        echo "  Start it first: cd $SHERLOCK_DIR/web && python main.py"
        exit 1
    fi
    ok "Sherlock web app is running"
}

# ─────────────────────────────────────────────────────────────
# Get admin JWT token
# ─────────────────────────────────────────────────────────────
get_token() {
    local user="${1:-admin}"
    local pass="${2:-}"

    if [ -z "$pass" ]; then
        echo -n "  Enter password for '$user': "
        read -rs pass
        echo
    fi

    TOKEN=$(curl -s -X POST "$API/api/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$user\",\"password\":\"$pass\"}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

    if [ -z "$TOKEN" ]; then
        fail "Login failed for '$user'"
        exit 1
    fi
    ok "Authenticated as $user"
}

AUTH() { echo "Authorization: Bearer $TOKEN"; }

# ─────────────────────────────────────────────────────────────
# LOAD
# ─────────────────────────────────────────────────────────────
do_load() {
    echo "━━━ Loading Sherlock Demo Environment ━━━"
    echo

    if [ -f "$DEMO_MARKER" ]; then
        warn "Demo already loaded (marker: $DEMO_MARKER)"
        echo "  Run '$0 unload' first to reset."
        exit 1
    fi

    check_api

    # Backup current config
    if [ -f "$CONF" ]; then
        cp "$CONF" "$CONF_BACKUP"
        ok "Backed up sherlock.conf → sherlock.conf.pre-demo"
    fi

    # Update NAS_PATHS to include demo folders
    DEMO_PATHS=$(echo "$DEMO_DATA" | tr ' ' ',')
    if grep -q "^NAS_PATHS=" "$CONF" 2>/dev/null; then
        # Save original NAS_PATHS
        ORIG_NAS=$(grep "^NAS_PATHS=" "$CONF" | cut -d= -f2-)
        echo "$ORIG_NAS" > "$DEMO_DIR/.orig-nas-paths"
        # Append demo paths
        if [ -n "$ORIG_NAS" ]; then
            sed -i '' "s|^NAS_PATHS=.*|NAS_PATHS=${ORIG_NAS},${DEMO_PATHS}|" "$CONF"
        else
            sed -i '' "s|^NAS_PATHS=.*|NAS_PATHS=${DEMO_PATHS}|" "$CONF"
        fi
    else
        echo "NAS_PATHS=$DEMO_PATHS" >> "$CONF"
    fi
    ok "Updated NAS_PATHS with demo case folders"

    # Authenticate
    echo
    echo "  Login required to create demo cases."
    get_token "admin"
    echo

    # Create demo cases via API
    echo "  Creating demo cases..."

    # Case 1: Martinez
    CASE1=$(curl -s -X POST "$API/api/cases" \
        -H "$(AUTH)" -H "Content-Type: application/json" \
        -d "{
            \"case_number\": \"26-CV-01847-RLR\",
            \"case_name\": \"Martinez v. Coastal Healthcare Systems\",
            \"case_type\": \"Medical Malpractice\",
            \"client_name\": \"Maria Elena Martinez\",
            \"opposing_party\": \"Coastal Healthcare Systems, Inc.\",
            \"jurisdiction\": \"S.D. Florida — Miami Division\",
            \"nas_path\": \"$DEMO_DIR/case-martinez\",
            \"description\": \"Bile duct injury during laparoscopic cholecystectomy. Plaintiff alleges failure to achieve critical view of safety. Two prior incidents involving same surgeon.\"
        }" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','FAIL'))" 2>/dev/null)
    [ "$CASE1" != "FAIL" ] && ok "Case 1: Martinez v. Coastal Healthcare (ID: $CASE1)" || warn "Case 1 may already exist"

    # Case 2: TechVault
    CASE2=$(curl -s -X POST "$API/api/cases" \
        -H "$(AUTH)" -H "Content-Type: application/json" \
        -d "{
            \"case_number\": \"26-CV-02391-EJD\",
            \"case_name\": \"TechVault Solutions v. NexGen Data Systems\",
            \"case_type\": \"Trade Secret / IP\",
            \"client_name\": \"TechVault Solutions, Inc.\",
            \"opposing_party\": \"NexGen Data Systems, LLC; Kevin P. Zhang\",
            \"jurisdiction\": \"N.D. California — San Jose Division\",
            \"nas_path\": \"$DEMO_DIR/case-techvault\",
            \"description\": \"Former senior engineer exfiltrated proprietary encryption source code and algorithm specs before joining direct competitor. TRO sought.\"
        }" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','FAIL'))" 2>/dev/null)
    [ "$CASE2" != "FAIL" ] && ok "Case 2: TechVault v. NexGen (ID: $CASE2)" || warn "Case 2 may already exist"

    # Case 3: Greenfield
    CASE3=$(curl -s -X POST "$API/api/cases" \
        -H "$(AUTH)" -H "Content-Type: application/json" \
        -d "{
            \"case_number\": \"GF-2025-001\",
            \"case_name\": \"Greenfield Industrial Partners — Riverside Development\",
            \"case_type\": \"Real Estate / Environmental\",
            \"client_name\": \"Riverside Development Group, Inc.\",
            \"opposing_party\": \"Greenfield Industrial Partners, LLC\",
            \"jurisdiction\": \"Georgia — Bibb County\",
            \"nas_path\": \"$DEMO_DIR/case-greenfield\",
            \"description\": \"Commercial property purchase with undisclosed environmental contamination. PCE/TCE in groundwater, heavy metals in soil. Negotiating remediation responsibility.\"
        }" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','FAIL'))" 2>/dev/null)
    [ "$CASE3" != "FAIL" ] && ok "Case 3: Greenfield / Riverside (ID: $CASE3)" || warn "Case 3 may already exist"

    echo
    echo "  Waiting for indexing to complete..."

    # Poll until indexing finishes (check every 5s, timeout after 3 min)
    TIMEOUT=180
    ELAPSED=0
    while [ $ELAPSED -lt $TIMEOUT ]; do
        # Check if any case is still indexing by looking at indexed_count
        COUNTS=$(curl -s "$API/api/cases" -H "$(AUTH)" \
            | python3 -c "
import sys, json
cases = json.load(sys.stdin)
total = sum(c.get('indexed_count', 0) for c in cases if c.get('case_number','').startswith(('26-CV','GF-')))
print(total)" 2>/dev/null)

        if [ "${COUNTS:-0}" -gt 0 ]; then
            ok "Indexed $COUNTS documents across demo cases"
            break
        fi
        sleep 5
        ELAPSED=$((ELAPSED + 5))
        echo -ne "  ⏳ Indexing... ${ELAPSED}s\r"
    done

    if [ $ELAPSED -ge $TIMEOUT ]; then
        warn "Indexing timeout — documents may still be processing in background"
    fi

    # Write marker
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEMO_MARKER"
    echo "$CASE1 $CASE2 $CASE3" >> "$DEMO_MARKER"

    echo
    echo "━━━ Demo Environment Loaded ━━━"
    echo
    echo "  3 cases created with $(find $DEMO_DIR/case-* -type f | wc -l | tr -d ' ') documents"
    echo "  See demo/DEMO_SCRIPT.md for the walkthrough"
    echo
}

# ─────────────────────────────────────────────────────────────
# UNLOAD
# ─────────────────────────────────────────────────────────────
do_unload() {
    echo "━━━ Unloading Sherlock Demo Environment ━━━"
    echo

    if [ ! -f "$DEMO_MARKER" ]; then
        warn "Demo not currently loaded (no marker file)"
        exit 0
    fi

    check_api

    # Authenticate
    echo "  Login required to remove demo cases."
    get_token "admin"
    echo

    # Read case IDs from marker
    CASE_IDS=$(tail -1 "$DEMO_MARKER" 2>/dev/null || echo "")

    # Delete demo cases via API
    echo "  Removing demo cases..."
    for CID in $CASE_IDS; do
        if [ "$CID" != "FAIL" ] && [ -n "$CID" ]; then
            HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$API/api/cases/$CID" \
                -H "$(AUTH)" 2>/dev/null)
            if [ "$HTTP" = "200" ] || [ "$HTTP" = "204" ]; then
                ok "Deleted case ID $CID"
            else
                warn "Could not delete case ID $CID (HTTP $HTTP) — may need manual cleanup"
            fi
        fi
    done

    # Restore config
    if [ -f "$CONF_BACKUP" ]; then
        cp "$CONF_BACKUP" "$CONF"
        rm "$CONF_BACKUP"
        ok "Restored sherlock.conf from backup"
    elif [ -f "$DEMO_DIR/.orig-nas-paths" ]; then
        ORIG_NAS=$(cat "$DEMO_DIR/.orig-nas-paths")
        sed -i '' "s|^NAS_PATHS=.*|NAS_PATHS=${ORIG_NAS}|" "$CONF"
        rm "$DEMO_DIR/.orig-nas-paths"
        ok "Restored original NAS_PATHS"
    fi

    # Clean up ChromaDB collections for demo cases
    echo "  Cleaning vector store collections..."
    for CID in $CASE_IDS; do
        if [ "$CID" != "FAIL" ] && [ -n "$CID" ]; then
            # Try to delete the collection via ChromaDB API
            curl -s -X DELETE "http://localhost:8000/api/v1/collections/case_${CID}_docs" \
                2>/dev/null && ok "Removed collection case_${CID}_docs" || true
        fi
    done

    # Clean up IndexedFile entries for demo paths
    if [ -f "$DB" ]; then
        sqlite3 "$DB" "DELETE FROM indexed_files WHERE file_path LIKE '%/demo/case-%';" 2>/dev/null
        ok "Cleaned demo entries from indexed_files table"
    fi

    # Remove marker
    rm -f "$DEMO_MARKER" "$DEMO_DIR/.orig-nas-paths"
    ok "Removed demo marker"

    echo
    echo "━━━ Demo Environment Unloaded ━━━"
    echo "  Production data and config restored."
    echo
}

# ─────────────────────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────────────────────
do_status() {
    if [ -f "$DEMO_MARKER" ]; then
        LOADED_AT=$(head -1 "$DEMO_MARKER")
        CASE_IDS=$(tail -1 "$DEMO_MARKER")
        echo -e "${GREEN}Demo is LOADED${NC}"
        echo "  Loaded at: $LOADED_AT"
        echo "  Case IDs: $CASE_IDS"
        echo "  Documents: $(find $DEMO_DIR/case-* -type f 2>/dev/null | wc -l | tr -d ' ') files"
    else
        echo -e "${YELLOW}Demo is NOT loaded${NC}"
        echo "  Run '$0 load' to install demo environment"
    fi
}

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
case "${1:-}" in
    load)   do_load ;;
    unload) do_unload ;;
    status) do_status ;;
    *)
        echo "Usage: $0 {load|unload|status}"
        echo
        echo "  load    — Create demo cases, index demo documents, update config"
        echo "  unload  — Remove demo cases, clean vectors, restore config"
        echo "  status  — Check if demo environment is currently loaded"
        exit 1
        ;;
esac
