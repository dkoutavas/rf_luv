#!/usr/bin/env bash
# Manage a single rotating decoder pipeline against the shared rf_luv data layer.
#
#   ./pipeline.sh up <pipe> [serial]      pause that dongle's scanner, bring <pipe> up
#   ./pipeline.sh down <pipe> [serial]    take <pipe> down, resume that dongle's scanner
#   ./pipeline.sh rotate <pipe> [serial]  take whatever pipeline is up down, bring <pipe> up
#   ./pipeline.sh logs <pipe>             tail the pipeline's container logs (no follow)
#   ./pipeline.sh ps <pipe>               show the pipeline's container status
#
# Valid pipes: acars adsb ais ism rds.  (Plus:  ./pipeline.sh demo)
#   - [serial] picks the dongle (default v4-01). Its rtl_tcp port comes from
#     /etc/rtl-scanner/<serial>.env and reaches the overlay as RTL_TCP_PORT.
#   - rtl_tcp serves one client at a time and the scanner holds its connection,
#     so 'up' pauses rtl-scanner@<serial> (ops/rf-mode pause) and 'down'
#     resumes it (ops/rf-mode scan). Pass the same serial to up and down.
#   - rds decodes the 57 kHz RDS subcarrier and needs a dongle with no FM notch:
#     since 2026-09-26 that is the V3, so: ./pipeline.sh up rds v3-01
#   - adsb opens the dongle over USB: readsb has no rtl_tcp input. 'up' stops
#     rtl_tcp for that serial (ops/rf-mode listen --force, which also stops the
#     scanner) and 'down' starts it again (ops/rf-mode scan).
#   - The spectrum scanner is not a pipe: it runs natively under systemd
#     (rtl-scanner@<serial>, ops/rtl-scanner).
#
# Guardrail: 'infra' is refused and project rf_luv_infra is never targeted, so
# this script can never tear down the always-on data layer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NET=rf_luv_net
CH_PING_URL="http://127.0.0.1:8123/ping"
VALID_PIPES="acars adsb ais ism rds pocsag"
# USB-mode pipes open the dongle directly (their decoder cannot read rtl_tcp),
# so 'up' frees it with `rf-mode listen` instead of taking the rtl_tcp path.
USB_PIPES="adsb pocsag"

DEFAULT_SERIAL=v4-01
RF_MODE="$SCRIPT_DIR/ops/rf-mode"

usage() {
    echo "usage: $0 up|down|rotate <pipe> [serial]  |  $0 logs|ps <pipe>  |  $0 demo" >&2
    echo "  pipes: $VALID_PIPES (and 'noaa' for the no-op systemd reminder)" >&2
    echo "  demo:  blind-analyze the bundled sample capture (no hardware needed)" >&2
    exit 2
}

# Guard a pipe name. 'infra' is hard-refused; only known pipes pass.
require_valid_pipe() {
    local pipe="$1"
    if [ "$pipe" = "infra" ]; then
        echo "[pipeline] refusing 'infra': the data layer is managed by infra/up.sh, not pipeline.sh" >&2
        exit 1
    fi
}

overlay_for() {
    echo "$SCRIPT_DIR/$1/compose.overlay.yml"
}

compose_pipe() {
    # compose_pipe <pipe> <up-args...|down-args...>
    local pipe="$1"; shift
    local project="rf_luv_${pipe}"
    local overlay; overlay="$(overlay_for "$pipe")"
    if [ "$project" = "rf_luv_infra" ]; then
        echo "[pipeline] internal guard: never operate on rf_luv_infra" >&2
        exit 1
    fi
    if [ ! -f "$overlay" ]; then
        echo "[pipeline] no overlay for '$pipe' at $overlay" >&2
        exit 1
    fi
    docker compose -p "$project" -f "$overlay" "$@"
}

# The dongle's rtl_tcp port, from its scanner env file.
dongle_port() {
    local envfile="/etc/rtl-scanner/$1.env" port
    port="$(grep -E '^RTL_TCP_PORT=' "$envfile" 2>/dev/null | cut -d= -f2 | tr -d ' ')" || true
    if [ -z "$port" ]; then
        echo "[pipeline] no RTL_TCP_PORT in $envfile (unknown dongle '$1'?)" >&2
        exit 1
    fi
    echo "$port"
}

# Lend the dongle to a decoder: the overlay connects to RTL_TCP_PORT, and the
# scanner must let go of rtl_tcp first.
claim_dongle() {
    local pipe="$1" serial="$2"
    case " $USB_PIPES " in *" $pipe "*)
        # The decoder opens the dongle over USB, so rtl_tcp must let go of it
        # entirely. --force: the serial may be the scanner's, and taking the
        # dongle from the scanner is what 'up' does for every pipe.
        ADSB_SERIAL="$serial"
        POCSAG_SERIAL="$serial"
        # Stamp the tuned POCSAG channel (env, e.g. 169.6M) as Hz for the row.
        POCSAG_FREQ_HZ="$(awk -v f="${POCSAG_FREQ:-169.6M}" 'BEGIN{s=toupper(f);u=substr(s,length(s),1);v=substr(s,1,length(s)-1);if(u=="M")print int(v*1e6);else if(u=="K")print int(v*1e3);else print int(s)}')"
        export ADSB_SERIAL POCSAG_SERIAL POCSAG_FREQ POCSAG_FREQ_HZ POCSAG_GAIN
        "$RF_MODE" listen "$serial" --force
        echo "[pipeline] $pipe uses $serial over USB"
        return
    ;; esac
    RTL_TCP_PORT="$(dongle_port "$serial")"
    # rds and acars label their rows with the dongle; follow the chosen one.
    RDS_DONGLE_ID="$serial"
    ACARS_DONGLE_ID="$serial"
    ACARS_FEED_ID="rf_luv-$serial"
    # AIS tuner gain: honor an operator override, else AIS-catcher AGC (auto).
    AIS_GAIN="${AIS_GAIN:-auto}"
    export RTL_TCP_PORT RDS_DONGLE_ID ACARS_DONGLE_ID ACARS_FEED_ID AIS_GAIN
    if systemctl --user is-active --quiet "rtl-scanner@${serial}.service"; then
        "$RF_MODE" pause "$serial"
    fi
    echo "[pipeline] $pipe uses $serial (rtl_tcp :$RTL_TCP_PORT)"
}

# Preflight: the shared network exists and ClickHouse answers /ping.
preflight() {
    if ! docker network inspect "$NET" >/dev/null 2>&1; then
        echo "[pipeline] network $NET missing; run infra/up.sh first" >&2
        exit 1
    fi
    if ! curl -sf "$CH_PING_URL" >/dev/null; then
        echo "[pipeline] ClickHouse not answering at $CH_PING_URL; run infra/up.sh first" >&2
        exit 1
    fi
}

# Which rotating pipeline is currently up? Derived live from running containers
# labelled with their compose project (NOT a flat state file). Prints the pipe
# name (e.g. 'acars') or nothing.
current_up_pipe() {
    local p
    for p in $VALID_PIPES; do
        if docker ps -q \
            --filter "label=com.docker.compose.project=rf_luv_${p}" \
            | grep -q .; then
            echo "$p"
            return 0
        fi
    done
    return 0
}

cmd_up() {
    local pipe="$1"
    require_valid_pipe "$pipe"
    # noaa has no rotating decoder container: it records via host systemd
    # (ops/noaa-pass-scheduler) and its schema is migrated by ch-bootstrap.
    if [ "$pipe" = "noaa" ]; then
        echo "[pipeline] noaa records via host systemd (ops/noaa-pass-scheduler); schema is already migrated by ch-bootstrap. Nothing to bring up."
        exit 0
    fi
    case " $VALID_PIPES " in
        *" $pipe "*) ;;
        *) echo "[pipeline] unknown pipe '$pipe' (valid: $VALID_PIPES)" >&2; exit 1 ;;
    esac
    preflight
    claim_dongle "$pipe" "$serial"
    echo "[pipeline] up rf_luv_${pipe}"
    compose_pipe "$pipe" up -d --build   # rebuild local images so code changes reach the container
}

cmd_down() {
    local pipe="$1"
    require_valid_pipe "$pipe"
    if [ "$pipe" = "noaa" ]; then
        echo "[pipeline] noaa has no compose project to take down (host systemd). Nothing to do."
        exit 0
    fi
    case " $VALID_PIPES " in
        *" $pipe "*) ;;
        *) echo "[pipeline] unknown pipe '$pipe' (valid: $VALID_PIPES)" >&2; exit 1 ;;
    esac
    echo "[pipeline] down rf_luv_${pipe}"
    compose_pipe "$pipe" down
    "$RF_MODE" scan "$serial"
}

cmd_rotate() {
    local target="$1"
    require_valid_pipe "$target"
    if [ "$target" = "noaa" ]; then
        echo "[pipeline] noaa records via host systemd (ops/noaa-pass-scheduler); schema is already migrated by ch-bootstrap. Nothing to rotate."
        exit 0
    fi
    case " $VALID_PIPES " in
        *" $target "*) ;;
        *) echo "[pipeline] unknown pipe '$target' (valid: $VALID_PIPES)" >&2; exit 1 ;;
    esac
    preflight
    local cur; cur="$(current_up_pipe)"
    # A USB-mode pipe (adsb, pocsag) holds the dongle with rtl_tcp stopped; the
    # rtl_tcp pipes need it running, and only 'down' (rf-mode scan) starts it
    # again. So refuse rotating out of a USB pipe.
    if [ -n "$cur" ] && [ "$cur" != "$target" ]; then
        case " $USB_PIPES " in *" $cur "*)
            echo "[pipeline] $cur holds $serial over USB: run '$0 down $cur $serial', then '$0 up $target $serial'" >&2
            exit 1
        ;; esac
    fi
    claim_dongle "$target" "$serial"
    if [ -n "$cur" ] && [ "$cur" != "$target" ]; then
        echo "[pipeline] rotating: $cur -> $target"
        compose_pipe "$cur" down
    elif [ "$cur" = "$target" ]; then
        echo "[pipeline] $target already up; re-applying"
    else
        echo "[pipeline] nothing currently up; bringing $target up"
    fi
    compose_pipe "$target" up -d --build
}

cmd_logs() {
    local pipe="$1"
    require_valid_pipe "$pipe"
    if [ "$pipe" = "noaa" ]; then
        echo "[pipeline] noaa runs under host systemd; use 'journalctl --user -u noaa-pass-scheduler'." >&2
        exit 0
    fi
    compose_pipe "$pipe" logs --no-color
}

cmd_ps() {
    local pipe="$1"
    require_valid_pipe "$pipe"
    if [ "$pipe" = "noaa" ]; then
        echo "[pipeline] noaa runs under host systemd; use 'systemctl --user list-timers'." >&2
        exit 0
    fi
    compose_pipe "$pipe" ps
}

# demo: run the blind analyzer on the bundled sample capture. No hardware, no
# Docker, no ClickHouse — the offline "it works" hit before any host bring-up.
cmd_demo() {
    local sample="$SCRIPT_DIR/samples/demo.cs8"
    if [ ! -f "$sample" ]; then
        echo "[pipeline] $sample missing; regenerate with: python3 samples/make_demo.py" >&2
        exit 1
    fi
    echo "[pipeline] blind signal autopsy of the bundled sample (no hardware needed):"
    python3 "$SCRIPT_DIR/spectrum/analysis/blind_analyze.py" --file "$sample" --dry-run
}

main() {
    if [ "${1:-}" = "demo" ]; then cmd_demo; return; fi
    [ $# -eq 2 ] || [ $# -eq 3 ] || usage
    local action="$1" pipe="$2"
    serial="${3:-$DEFAULT_SERIAL}"
    case "$action" in
        up)     cmd_up "$pipe" ;;
        down)   cmd_down "$pipe" ;;
        rotate) cmd_rotate "$pipe" ;;
        logs)   cmd_logs "$pipe" ;;
        ps)     cmd_ps "$pipe" ;;
        *)      usage ;;
    esac
}

main "$@"
