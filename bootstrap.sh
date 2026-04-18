#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════╗
# ║  rf_luv bootstrap — prep a fresh clone                  ║
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
echo "    Windows: follow setup/install-windows.md (Zadig → WinUSB → rtl_tcp.exe)"
echo ""
echo "  Client (Docker host running the pipeline, can be same machine):"
echo "    bash setup/install-wsl.sh               # WSL/openSUSE toolchain (optional)"
echo "    cd spectrum && docker compose up -d     # primary spectrum pipeline"
echo "    open http://localhost:3003              # Grafana dashboards"
echo ""
echo "  Companion pipelines (one at a time — single dongle):"
echo "    cd adsb  && docker compose up -d        # aircraft (tar1090 :8080, Grafana :3000)"
echo "    cd ais   && docker compose up -d        # ships (Grafana :3001)"
echo "    cd ism   && docker compose up -d        # ISM 433 MHz (Grafana :3002)"
echo ""
