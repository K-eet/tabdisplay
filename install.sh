#!/usr/bin/env bash
# Install tabdisplay so that plugging the tablet in is all it takes.
#
#   ./install.sh            # tablet attached with USB debugging on
#   ./install.sh --no-apk   # host side only
#   ./install.sh --remove   # undo the service and the udev rule
#
# What it sets up:
#   host/tdcap                              built from source
#   the receiver app                        built and installed over adb
#   ~/.config/systemd/user/tabdisplay.service
#   /etc/udev/rules.d/71-tabdisplay.rules   (needs sudo)
#
# The udev rule tags this one tablet (by USB serial) so that the systemd
# user manager starts tabdisplay.service when it appears; the service is
# bound to the device, so it stops when the cable is pulled.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/tabdisplay.service"
RULE=/etc/udev/rules.d/71-tabdisplay.rules

if [[ "${1:-}" == "--remove" ]]; then
    systemctl --user stop tabdisplay.service 2>/dev/null || true
    rm -f "$UNIT"
    sudo rm -f "$RULE"
    sudo udevadm control --reload
    systemctl --user daemon-reload
    echo "removed $UNIT and $RULE"
    exit 0
fi

APK=1
[[ "${1:-}" == "--no-apk" ]] && APK=0

SERIAL="${ADB_SERIAL:-$(adb get-serialno 2>/dev/null || true)}"
if [[ -z "$SERIAL" || "$SERIAL" == "unknown" ]]; then
    echo "No tablet found over adb. Plug it in with USB debugging enabled," >&2
    echo "or set ADB_SERIAL=<usb serial>." >&2
    exit 1
fi
echo "tablet serial: $SERIAL"

echo "building host/tdcap"
make -s -C "$HERE/host"

if [[ $APK -eq 1 ]]; then
    echo "building and installing the app"
    (cd "$HERE/android" &&
        JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}" \
            ./gradlew -q assembleDebug)
    adb -s "$SERIAL" install -r \
        "$HERE/android/app/build/outputs/apk/debug/app-debug.apk"
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=tabdisplay: tablet as a second monitor
# Lives exactly as long as the tablet is on the bus.
BindsTo=dev-tabdisplay.device
After=dev-tabdisplay.device

[Service]
ExecStart=/usr/bin/python3 $HERE/host/tabdisplayd.py --serial $SERIAL
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=1
UNIT_EOF
echo "wrote $UNIT"

sudo tee "$RULE" >/dev/null <<RULE_EOF
# tabdisplay: start the user service when this tablet is plugged in.
# ID_MM_DEVICE_IGNORE keeps ModemManager from probing the tablet's serial
# interface with AT commands while adb is coming up.
ACTION!="remove", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{serial}=="$SERIAL", TAG+="systemd", SYMLINK+="tabdisplay", ENV{SYSTEMD_USER_WANTS}+="tabdisplay.service", ENV{ID_MM_DEVICE_IGNORE}="1"
RULE_EOF
echo "wrote $RULE"

sudo udevadm control --reload
systemctl --user daemon-reload
sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=serial="$SERIAL"
sleep 1
systemctl --user --no-pager --lines=0 status tabdisplay.service || true
echo
echo "Done. Unplug and replug the tablet any time."
echo "Logs: journalctl --user -u tabdisplay -f"
