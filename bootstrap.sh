#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════╗
# ║  rf_luv bootstrap — organize files & prep environment   ║
# ║  Run once: bash bootstrap.sh                            ║
# ╚══════════════════════════════════════════════════════════╝

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── 1. Kill Zone.Identifier files ──────────────────────
echo ""
echo "=== Cleaning Zone.Identifier files ==="
ZI_COUNT=$(find . -name '*:Zone.Identifier' 2>/dev/null | wc -l)
if [ "$ZI_COUNT" -gt 0 ]; then
    find . -name '*:Zone.Identifier' -delete
    info "Deleted $ZI_COUNT Zone.Identifier files"
else
    info "No Zone.Identifier files found"
fi

# ─── 2. Create directory structure ──────────────────────
echo ""
echo "=== Creating directory structure ==="
mkdir -p setup
mkdir -p adsb/clickhouse
mkdir -p adsb/grafana/provisioning/datasources
mkdir -p adsb/grafana/provisioning/dashboards/json
mkdir -p scripts
mkdir -p config
mkdir -p recordings
mkdir -p notes
info "Directories created"

# ─── 3. Move files to correct locations ─────────────────
echo ""
echo "=== Organizing files ==="

move_if_flat() {
    local file="$1"
    local dest="$2"
    # Only move if file exists at root AND destination doesn't already have it
    if [ -f "$SCRIPT_DIR/$file" ] && [ "$SCRIPT_DIR/$file" != "$SCRIPT_DIR/$dest" ]; then
        mv "$SCRIPT_DIR/$file" "$SCRIPT_DIR/$dest"
        info "Moved $file → $dest"
    fi
}

# Setup files
move_if_flat "install-wsl.sh" "setup/install-wsl.sh"
move_if_flat "install-windows.md" "setup/install-windows.md"

# ADS-B pipeline
move_if_flat "docker-compose.yml" "adsb/docker-compose.yml"
move_if_flat "Dockerfile.ingest" "adsb/Dockerfile.ingest"
move_if_flat "ingest.py" "adsb/ingest.py"
move_if_flat "init.sql" "adsb/clickhouse/init.sql"
move_if_flat "clickhouse.yml" "adsb/grafana/provisioning/datasources/clickhouse.yml"
move_if_flat "dashboards.yml" "adsb/grafana/provisioning/dashboards/dashboards.yml"
move_if_flat "adsb-overview.json" "adsb/grafana/provisioning/dashboards/json/adsb-overview.json"

# Scripts
move_if_flat "spectrum-scan.sh" "scripts/spectrum-scan.sh"
move_if_flat "satellite-pass.sh" "scripts/satellite-pass.sh"
move_if_flat "ism-monitor.sh" "scripts/ism-monitor.sh"
move_if_flat "ais-monitor.sh" "scripts/ais-monitor.sh"
move_if_flat "airband-listen.sh" "scripts/airband-listen.sh"

# ─── 4. Set permissions ────────────────────────────────
echo ""
echo "=== Setting permissions ==="
find . -name '*.sh' -exec chmod +x {} \;
info "All .sh files marked executable"

# ─── 5. Git init (if not already) ──────────────────────
echo ""
echo "=== Git ==="
if [ ! -d ".git" ]; then
    git init -q
    info "Git repo initialized"
else
    info "Git repo already exists"
fi

# ─── 6. Verify structure ───────────────────────────────
echo ""
echo "=== Final structure ==="
find . -type f -not -path './.git/*' -not -name '*.gitkeep' | sort | \
    sed 's|^\./||' | while read -r f; do echo "  $f"; done

echo ""
info "Bootstrap complete. Next steps:"
echo ""
echo "  Before dongle arrives:"
echo "    bash setup/install-wsl.sh          # install SDR packages"
echo "    # Install SDR++ and Zadig on Windows (see setup/install-windows.md)"
echo ""
echo "  When dongle arrives:"
echo "    # 1. Zadig → WinUSB driver"
echo "    # 2. SDR++ → tune 100 MHz → hear FM"
echo "    # 3. Start ADS-B pipeline:"
echo "    #    Windows: rtl_tcp -a 0.0.0.0 -p 1234 -s 2048000"
echo "    #    WSL:     cd adsb && docker compose up -d"
echo "    #    Browser: localhost:8080 (map) / localhost:3000 (grafana)"
echo ""
