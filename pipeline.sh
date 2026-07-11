#!/usr/bin/env bash
# Manage a single rotating decoder pipeline against the shared rf_luv data layer.
#
#   ./pipeline.sh up <pipe>        bring <pipe> up   (compose project rf_luv_<pipe>)
#   ./pipeline.sh down <pipe>      take <pipe> down
#   ./pipeline.sh rotate <pipe>    take whatever V4 pipeline is up down, bring <pipe> up
#   ./pipeline.sh logs <pipe>      tail the pipeline's container logs (no follow)
#   ./pipeline.sh ps <pipe>        show the pipeline's container status
#
# Valid pipes: acars adsb ais ism rds spectrum.  (Plus:  ./pipeline.sh demo)
#   - One local V4 = one rtl_tcp (host.docker.internal:1234), single-tenant per
#     session: the box is EITHER sweeping OR running one decoder, never both. The
#     five decoders (acars/adsb/ais/ism/rds) share that one dongle, so only one
#     runs at a time; 'rotate' swaps them.
#     rds decodes the 57 kHz RDS subcarrier and needs the V4 with the FM notch
#     REMOVED (with the notch on, the FM band is attenuated and RDS is invisible).
#   - spectrum's overlay is the profile-gated containerized scanner (a smoke-test);
#     the steady scanner runs as native systemd on the local host (ops/rtl-scanner).
#
# Guardrail: 'infra' is refused and project rf_luv_infra is never targeted, so
# this script can never tear down the always-on data layer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NET=rf_luv_net
CH_PING_URL="http://127.0.0.1:8123/ping"
VALID_PIPES="acars adsb ais ism rds spectrum"

usage() {
    echo "usage: $0 up|down|rotate|logs|ps <pipe>   |   $0 demo" >&2
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
    echo "[pipeline] up rf_luv_${pipe}"
    compose_pipe "$pipe" up -d
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
    if [ -n "$cur" ] && [ "$cur" != "$target" ]; then
        echo "[pipeline] rotating: $cur -> $target"
        compose_pipe "$cur" down
    elif [ "$cur" = "$target" ]; then
        echo "[pipeline] $target already up; re-applying"
    else
        echo "[pipeline] nothing currently up; bringing $target up"
    fi
    compose_pipe "$target" up -d
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
    [ $# -eq 2 ] || usage
    local action="$1" pipe="$2"
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
