#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════╗
# ║  rf_luv bootstrap - prep a fresh clone                  ║
# ║  Run once: bash bootstrap.sh                            ║
# ╚══════════════════════════════════════════════════════════╝

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── 1. Strip WSL/Windows Zone.Identifier metadata ───────
echo ""
echo "=== Cleaning Zone.Identifier files ==="
ZI_COUNT=$(find . -name '*:Zone.Identifier' 2>/dev/null | wc -l)
if [ "$ZI_COUNT" -gt 0 ]; then
    find . -name '*:Zone.Identifier' -delete
    info "Deleted $ZI_COUNT Zone.Identifier files"
else
    info "No Zone.Identifier files found"
fi

# ─── 2. Mark shell scripts executable ────────────────────
echo ""
echo "=== Setting permissions ==="
find . -name '*.sh' -not -path './.git/*' -exec chmod +x {} \;
info "All .sh files marked executable"

# ─── 3. Git init (if not already) ────────────────────────
echo ""
echo "=== Git ==="
if [ ! -d ".git" ]; then
    git init -q
    info "Git repo initialized"
else
    info "Git repo already exists"
fi

# ─── 4. Next steps ───────────────────────────────────────
echo ""
info "Bootstrap complete. Next steps:"
echo ""
echo "  Host (where the RTL-SDR is plugged in):"
echo "    Linux:   bash ops/rtl-tcp/install.sh    # systemd rtl_tcp + watchdog"
echo "    Windows: follow setup/install-windows.md (Zadig, WinUSB, rtl_tcp.exe)"
echo ""
echo "  Shared data layer (Docker host, can be same machine):"
echo "    bash setup/install-wsl.sh               # WSL/openSUSE toolchain (optional)"
echo "    docker network create rf_luv_net        # once"
echo "    bash infra/up.sh                        # shared ClickHouse 8123/9000 + Grafana 3000"
echo "    open http://localhost:3000              # Grafana dashboards (one folder per pipeline)"
echo ""
echo "  Rotating V4 decoders (one decoder per dongle at a time):"
echo "    bash pipeline.sh up adsb                # aircraft (tar1090 :8080, ADS-B folder)"
echo "    bash pipeline.sh up ais                 # ships (AIS folder)"
echo "    bash pipeline.sh up ism                 # ISM 433 MHz (ISM folder)"
echo "    bash pipeline.sh up acars               # ACARS (ACARS folder)"
echo ""
