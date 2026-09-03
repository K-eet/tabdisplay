#!/usr/bin/env python3
"""
Phase 1 verification: open evdi card 0, attach an EDID, drive handle_events,
confirm at least one PyEvdi callback fires, snapshot GNOME's view of outputs
before and after connect to confirm mutter sees the virtual display, and
disconnect cleanly.

Run from /home/user/projects/tabdisplay with the repo's pyevdi build on
PYTHONPATH:
  PYTHONPATH=evdi-src/pyevdi python3 test_pyevdi.py
"""
import os
import subprocess
import sys
import time

import PyEvdi


EDID_PATH = "evdi-src/pyevdi/sample_edid/4K60HzTest.edid"
EVENT_LOOP_SECONDS = 6.0
HANDLE_EVENT_TIMEOUT_MS = 500


def gnome_outputs():
    """Return mutter's current connector list (best-effort, plain stdout)."""
    try:
        out = subprocess.check_output(
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.Mutter.DisplayConfig",
                "--object-path", "/org/gnome/Mutter/DisplayConfig",
                "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState",
            ],
            stderr=subprocess.STDOUT, timeout=5,
        ).decode()
    except Exception as e:
        return f"<gdbus failed: {e}>"
    connectors = []
    for tok in out.split("'"):
        if tok in {"eDP-1", "DVI-I-1", "HDMI-A-1", "DP-1", "DP-2", "Virtual-1"}:
            if tok not in connectors:
                connectors.append(tok)
    return ", ".join(connectors) if connectors else out[:200]


def main():
    print(f"[setup] PyEvdi version: {PyEvdi.version}")
    print(f"[setup] check_device(0): {PyEvdi.check_device(0)}")
    if PyEvdi.check_device(0) != PyEvdi.AVAILABLE:
        print("[fatal] evdi card 0 not AVAILABLE", file=sys.stderr)
        return 2

    with open(EDID_PATH, "rb") as fh:
        edid = fh.read()
    print(f"[setup] EDID loaded: {len(edid)} bytes")

    counts = {"mode_changed": 0, "framebuffer": 0}

    def on_mode_changed(mode):
        counts["mode_changed"] += 1
        print(f"[cb mode_changed #{counts['mode_changed']}] "
              f"{mode.width}x{mode.height}@{mode.refresh_rate}")

    def on_framebuffer(buffer):
        counts["framebuffer"] += 1
        print(f"[cb framebuffer  #{counts['framebuffer']}] "
              f"id={buffer.id} rects={buffer.rect_count}")

    print(f"[gnome before connect] outputs: {gnome_outputs()}")

    card = PyEvdi.Card(0)
    card.mode_changed_handler = on_mode_changed
    card.acquire_framebuffer_handler = on_framebuffer

    full_hd_area = 1920 * 1080
    full_hd_pclk = full_hd_area * 60
    card.connect(edid, len(edid), full_hd_area, full_hd_pclk)
    print("[connect] EDID attached; pumping events…")

    time.sleep(0.5)
    print(f"[gnome after connect ] outputs: {gnome_outputs()}")

    deadline = time.time() + EVENT_LOOP_SECONDS
    while time.time() < deadline:
        card.handle_events(HANDLE_EVENT_TIMEOUT_MS)

    card.disconnect()
    card.close()
    print(f"[gnome after disconnect] outputs: {gnome_outputs()}")

    print(f"[result] mode_changed fired: {counts['mode_changed']}  "
          f"framebuffer fired: {counts['framebuffer']}")
    if counts["mode_changed"] == 0 and counts["framebuffer"] == 0:
        print("[fail] no callbacks fired", file=sys.stderr)
        return 1
    print("[pass] at least one callback fired -> evdi pipeline is live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
