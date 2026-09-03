#!/usr/bin/env bash
# Bring up the tabdisplay extended-display pipeline.
#
# Usage:
#   ./start.sh                 # v2 daemon (VAAPI + MediaCodec), default
#   ./start.sh --legacy        # v1 daemon (raw RGBA, dirty rects)
#   ./start.sh --no-app        # skip relaunching the Android app
#   ./start.sh --no-reverse    # skip 'adb reverse'
#
# All remaining flags pass through to the daemon (e.g. --bitrate 15M,
# --pixel-format nv12 to force the slow numpy baseline).

set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT"

ADB_SERIAL="${ADB_SERIAL:-TABLET_SERIAL}"
PORT="${TABDISPLAY_PORT:-7070}"
ACTIVITY="com.tabdisplay/.ReceiverActivity_v2"

USE_LEGACY=0
LAUNCH_APP=1
SETUP_REVERSE=1
PASSTHROUGH=()
for arg in "$@"; do
    case "$arg" in
        --legacy)      USE_LEGACY=1 ;;
        --no-app)      LAUNCH_APP=0 ;;
        --no-reverse)  SETUP_REVERSE=0 ;;
        *)             PASSTHROUGH+=("$arg") ;;
    esac
done

if [[ $USE_LEGACY -eq 1 ]]; then
    DAEMON="daemon_v1.py"
    ACTIVITY="com.tabdisplay/.ReceiverActivity"
    echo "[start.sh] using v1 daemon (raw RGBA + dirty rects)"
else
    DAEMON="daemon_v2.py"
    echo "[start.sh] using v2 daemon (VAAPI H.264 + MediaCodec)"
fi

if [[ $SETUP_REVERSE -eq 1 ]]; then
    # Non-fatal on purpose: the watchdog below keeps re-arming every 10 s,
    # so the tablet may be plugged in any time after startup.
    reverse_ok=0
    if adb -s "$ADB_SERIAL" reverse "tcp:${PORT}" "tcp:${PORT}" >/dev/null 2>&1; then
        echo "[start.sh] adb reverse tcp:${PORT} tcp:${PORT}"
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
            if adb -s "$ADB_SERIAL" reverse "tcp:${PORT}" "tcp:${PORT}" >/dev/null 2>&1; then
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
    python3 "$PROJECT/$DAEMON" "${PASSTHROUGH[@]}"
