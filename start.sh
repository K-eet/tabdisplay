#!/usr/bin/env bash
# Bring up the tabdisplay extended-display pipeline.
#
# Usage:
#   ./start.sh                 # daemon + adb reverse + relaunch the app
#   ./start.sh --no-app        # skip relaunching the Android app
#   ./start.sh --no-reverse    # skip 'adb reverse'
#
# Environment:
#   ADB_SERIAL        tablet to talk to (default below)
#   TABDISPLAY_PORT   TCP port on the PC side (default 7070; the app always
#                     connects to its own localhost:7070)
#
# All remaining flags pass through to the daemon (e.g. --bitrate 15M,
# --no-auto-mode).

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT"

ADB_SERIAL="${ADB_SERIAL:-TABLET_SERIAL}"
PORT="${TABDISPLAY_PORT:-7070}"
APP_PORT=7070
ACTIVITY="com.tabdisplay/.ReceiverActivity_v2"
DAEMON="daemon_v2.py"

LAUNCH_APP=1
SETUP_REVERSE=1
PASSTHROUGH=()
for arg in "$@"; do
    case "$arg" in
        --no-app)      LAUNCH_APP=0 ;;
        --no-reverse)  SETUP_REVERSE=0 ;;
        *)             PASSTHROUGH+=("$arg") ;;
    esac
done

arm_reverse() {
    adb -s "$ADB_SERIAL" reverse "tcp:${APP_PORT}" "tcp:${PORT}" >/dev/null 2>&1
}

if [[ $SETUP_REVERSE -eq 1 ]]; then
    # Non-fatal on purpose: the watchdog below keeps re-arming every 3 s,
    # so the tablet may be plugged in any time after startup.
    reverse_ok=0
    if arm_reverse; then
        echo "[start.sh] adb reverse tcp:${APP_PORT} tcp:${PORT}"
        reverse_ok=1
    else
        echo "[start.sh] WARN: initial adb reverse failed (tablet offline?); watchdog will retry"
    fi

    # Watchdog: 'adb reverse' rules are lost when the tablet unplugs/replugs
    # or renegotiates USB. Re-arm every 3 s for as long as the daemon runs
    # (short interval directly bounds reconnect wait); print only on state
    # transitions so a disconnected tablet doesn't spam.
    (
        ok=$reverse_ok
        while kill -0 "$$" 2>/dev/null; do
            sleep 3
            kill -0 "$$" 2>/dev/null || break
            if arm_reverse; then
                if [[ $ok -eq 0 ]]; then
                    echo "[start.sh] adb reverse armed"
                    ok=1
                fi
            else
                if [[ $ok -eq 1 ]]; then
                    echo "[start.sh] WARN: adb unreachable; will keep retrying reverse"
                    ok=0
                fi
            fi
        done
    ) &
fi

if [[ $LAUNCH_APP -eq 1 ]]; then
    if ! adb -s "$ADB_SERIAL" shell am force-stop com.tabdisplay >/dev/null 2>&1 || \
       ! adb -s "$ADB_SERIAL" shell am start -W -n "$ACTIVITY" >/dev/null 2>&1; then
        echo "[start.sh] WARN: could not launch ${ACTIVITY} (tablet offline?)"
    else
        echo "[start.sh] relaunching ${ACTIVITY}"
    fi
fi

echo "[start.sh] starting $DAEMON (Ctrl-C to stop)"
exec env PYTHONPATH="$PROJECT/evdi-src/pyevdi" \
    python3 "$PROJECT/$DAEMON" --port "$PORT" "${PASSTHROUGH[@]}"
