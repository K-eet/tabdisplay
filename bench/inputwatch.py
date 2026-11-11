#!/usr/bin/env python3
"""Full-screen window that prints the touch and pen events it receives.

    bench/inputwatch.py <text matched against connector/model> [seconds]

Coordinates are printed as a fraction of the window, so with the window
covering the tablet's monitor they can be compared directly with what the
tablet (or bench/fakeclient.py) sent.
"""
import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

NAMES = {Gdk.EventType.TOUCH_BEGIN: "touch-begin", Gdk.EventType.TOUCH_UPDATE: "touch-update",
         Gdk.EventType.TOUCH_END: "touch-end", Gdk.EventType.TOUCH_CANCEL: "touch-cancel",
         Gdk.EventType.BUTTON_PRESS: "press", Gdk.EventType.BUTTON_RELEASE: "release",
         Gdk.EventType.MOTION_NOTIFY: "motion", Gdk.EventType.PROXIMITY_IN: "proximity-in",
         Gdk.EventType.PROXIMITY_OUT: "proximity-out"}


def main():
    want = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    app = Gtk.Application(application_id="com.tabdisplay.bench.inputwatch")

    def activate(app):
        win = Gtk.ApplicationWindow(application=app, title="tabdisplay input")
        area = Gtk.Label(label="touch or draw here")
        win.set_child(area)

        def on_event(ctl, _event):
            # PyGObject hands the signal's event over as None; ask for it.
            event = ctl.get_current_event()
            if event is None:
                return False
            kind = NAMES.get(event.get_event_type())
            if kind is None:
                return False
            ok, x, y = event.get_position()
            tool = event.get_device_tool()
            line = "%s %.3f %.3f" % (kind, x / max(1, win.get_width()),
                                     y / max(1, win.get_height()))
            if tool is not None:
                line += " tool=%s" % tool.get_tool_type().value_nick
                got, p = event.get_axis(Gdk.AxisUse.PRESSURE)
                if got:
                    line += " pressure=%.2f" % p
                got, t = event.get_axis(Gdk.AxisUse.XTILT)
                if got:
                    line += " xtilt=%.2f" % t
            elif kind.startswith("touch"):
                line += " seq=%s" % (hash(event.get_event_sequence()) % 1000)
            else:
                src = event.get_device()
                line += " source=%s" % (src.get_source().value_nick if src else "?")
            print(line, flush=True)
            return False

        ctl = Gtk.EventControllerLegacy()
        ctl.connect("event", on_event)
        win.add_controller(ctl)
        for m in Gdk.Display.get_default().get_monitors():
            desc = f"{m.get_connector()} {m.get_model()} {m.get_manufacturer()}"
            if want and want in desc.lower():
                win.fullscreen_on_monitor(m)
                win.present()
                if secs:
                    GLib.timeout_add(int(secs * 1000), app.quit)
                return
        print("no monitor matched", repr(want), file=sys.stderr)
        app.quit()

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
