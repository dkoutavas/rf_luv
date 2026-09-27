#!/usr/bin/env bash
set -euo pipefail

# RTL-SDR Lab - openSUSE Tumbleweed package setup
# Run: bash setup/install-packages.sh
#
# Installs the SDR toolchain (rtl-sdr, decoders, audio and Python tools) on
# the native Tumbleweed host, where the dongles sit on USB directly. The
# services come from ops/install-host.sh; RESTORE.md has the full order.

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; }
step()  { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

# Track what succeeded/failed for summary
INSTALLED=()
SKIPPED=()
FAILED=()

try_zypper() {
    local pkg="$1"
    local desc="${2:-$1}"
    if rpm -q "$pkg" &>/dev/null; then
        SKIPPED+=("$desc (already installed)")
        return 0
    fi
    if zypper se -x "$pkg" &>/dev/null; then
        if sudo zypper install -y "$pkg" &>/dev/null; then
            INSTALLED+=("$desc")
            return 0
        fi
    fi
    FAILED+=("$desc ($pkg not found or install failed)")
    return 1
}

try_pip() {
    local pkg="$1"
    local desc="${2:-$1}"
    if python3 -m pip show "$pkg" &>/dev/null 2>&1; then
        SKIPPED+=("$desc (already installed)")
        return 0
    fi
    if python3 -m pip install --user --break-system-packages "$pkg" &>/dev/null 2>&1; then
        INSTALLED+=("$desc")
        return 0
    fi
    FAILED+=("$desc (pip install failed)")
    return 1
}

build_from_source() {
    local name="$1"
    local repo="$2"
    local dir="/tmp/build-$name"

    if command -v "$name" &>/dev/null; then
        SKIPPED+=("$name (already available)")
        return 0
    fi

    warn "Building $name from source..."
    rm -rf "$dir"
    if git clone --depth 1 "$repo" "$dir" && cd "$dir"; then
        if [ -f CMakeLists.txt ]; then
            mkdir -p build && cd build && cmake .. && make -j"$(nproc)" && sudo make install
        elif [ -f Makefile ]; then
            make -j"$(nproc)" && sudo make install
        else
            FAILED+=("$name (no CMakeLists.txt or Makefile found)")
            cd /; rm -rf "$dir"
            return 1
        fi
        if [ $? -eq 0 ]; then
            INSTALLED+=("$name (built from source)")
            cd /; rm -rf "$dir"
            return 0
        fi
    fi
    FAILED+=("$name (build failed - check dependencies)")
    cd /
    rm -rf "$dir"
    return 1
}

# ─── Preflight ───────────────────────────────────────────────

step "Preflight checks"

if ! command -v zypper &>/dev/null; then
    err "This script is for openSUSE. Exiting."
    exit 1
fi
info "openSUSE detected"

if ! command -v git &>/dev/null; then
    sudo zypper install -y git
fi
info "git available"

if ! command -v cmake &>/dev/null; then
    sudo zypper install -y cmake gcc gcc-c++ make
fi
info "Build tools available"

if ! command -v python3 &>/dev/null; then
    sudo zypper install -y python3 python3-pip
fi
info "Python 3 available"

# ─── Core SDR Libraries ─────────────────────────────────────

step "Core SDR libraries"

try_zypper rtl-sdr "rtl-sdr (base tools: rtl_fm, rtl_power, rtl_tcp, etc.)" || true
try_zypper rtl-sdr-devel "rtl-sdr-devel (development headers)" || true
try_zypper soapy-sdr-devel "SoapySDR (hardware abstraction layer)" || true
try_zypper soapysdr0.8-3-module-rtlsdr "SoapySDR RTL-SDR module" || true

# ─── GNU Radio ───────────────────────────────────────────────

step "GNU Radio (signal processing framework)"

try_zypper gnuradio "GNU Radio" || true

# ─── Decoders & Tools ────────────────────────────────────────

step "Decoders and signal processing tools"

try_zypper multimon-ng "multimon-ng (POCSAG, FLEX, EAS, DTMF decoder)" || \
    build_from_source multimon-ng "https://github.com/EliasOeworka/multimon-ng"

try_zypper sox "SoX (audio processing - useful for piping/converting SDR audio)" || true
try_zypper ffmpeg-8 "FFmpeg (media processing, format conversion)" || \
    try_zypper ffmpeg-7 "FFmpeg (media processing, format conversion)" || true

# rtl_433 - ISM band decoder (weather stations, sensors, car keyfobs)
try_zypper rtl_433 "rtl_433 (ISM band protocol decoder)" || \
    build_from_source rtl_433 "https://github.com/merbanan/rtl_433"

# ─── ADS-B Tools ─────────────────────────────────────────────

step "ADS-B / aviation tools"

# readsb will run in Docker (see adsb/compose.overlay.yml)
# but install dump1090 CLI tools if available
try_zypper dump1090 "dump1090 (Mode S / ADS-B decoder)" || \
    warn "dump1090 not in repos - will use Docker readsb instead (recommended)"

# ─── Satellite Tools ─────────────────────────────────────────

step "Satellite tracking and decoding"

try_zypper gpredict "gpredict (satellite pass prediction GUI)" || true
try_zypper predict "predict (CLI satellite tracking)" || true
try_pip orbit-predictor "orbit-predictor (Python satellite prediction library)" || true

# satdump - modern satellite decoder (NOAA APT, Meteor LRPT, etc.)
# Usually needs to be built from source or grabbed as AppImage
if ! command -v satdump &>/dev/null; then
    warn "satdump not in repos - grab AppImage from github.com/SatDump/SatDump/releases"
    warn "  or build from source: https://github.com/SatDump/SatDump"
    FAILED+=("satdump (manual install needed - see above)")
fi

# ─── Digital Mode Tools ──────────────────────────────────────

step "Digital mode decoders"

# AIS - ship tracking
try_zypper rtl-ais "rtl-ais (AIS ship tracking decoder)" || \
    build_from_source rtl-ais "https://github.com/dgiardini/rtl-ais" || \
    warn "rtl-ais: try 'pip install pyais' for Python AIS decoding instead"

# ─── Data Pipeline Tools ─────────────────────────────────────

step "Data pipeline and visualization"

if command -v docker &>/dev/null; then
    info "Docker available"
else
    warn "Docker not found - see RESTORE.md step 1 (zypper install docker docker-compose)"
    FAILED+=("Docker (needed for the shared data layer and the decoders)")
fi

if command -v docker-compose &>/dev/null || docker compose version &>/dev/null 2>&1; then
    info "Docker Compose available"
else
    warn "Docker Compose not found"
    FAILED+=("Docker Compose")
fi

# clickhouse-client for ad-hoc queries
try_zypper clickhouse-client "clickhouse-client (CLI for ClickHouse queries)" || \
    warn "clickhouse-client not in repos - will use Docker exec instead"

# Python libs for data processing
try_pip clickhouse-connect "clickhouse-connect (Python ClickHouse client)" || true
try_pip requests "requests (HTTP client)" || true

# ─── Spectrum Analysis Tools ─────────────────────────────────

step "Spectrum analysis utilities"

# rtl_power is included with rtl-sdr package
# heatmap.py for visualizing rtl_power output
if [ ! -f "$HOME/.local/bin/heatmap.py" ]; then
    mkdir -p "$HOME/.local/bin"
    if curl -sL "https://raw.githubusercontent.com/keenerd/rtl-sdr-misc/master/heatmap/heatmap.py" \
        -o "$HOME/.local/bin/heatmap.py" 2>/dev/null; then
        chmod +x "$HOME/.local/bin/heatmap.py"
        INSTALLED+=("heatmap.py (rtl_power visualization)")
    else
        warn "Could not download heatmap.py - grab it manually from keenerd/rtl-sdr-misc on GitHub"
        FAILED+=("heatmap.py (download failed)")
    fi
else
    SKIPPED+=("heatmap.py (already exists)")
fi

# ─── Summary ─────────────────────────────────────────────────

step "Installation Summary"

if [ ${#INSTALLED[@]} -gt 0 ]; then
    echo -e "${GREEN}Installed:${NC}"
    for item in "${INSTALLED[@]}"; do echo "  ✓ $item"; done
fi

if [ ${#SKIPPED[@]} -gt 0 ]; then
    echo -e "${YELLOW}Skipped:${NC}"
    for item in "${SKIPPED[@]}"; do echo "  ○ $item"; done
fi

if [ ${#FAILED[@]} -gt 0 ]; then
    echo -e "${RED}Needs attention:${NC}"
    for item in "${FAILED[@]}"; do echo "  ✗ $item"; done
fi

echo ""
info "Package setup complete. Next: RESTORE.md step 3 (dongle serials), then infra/up.sh and ops/install-host.sh."
