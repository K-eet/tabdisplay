#!/usr/bin/env python3
"""Full-screen vertical stripes, each asking for a different cursor shape.

    bench/cursors.py <text matched against connector/model> [seconds]

Sweeping the pointer across it makes the compositor change the pointer
image many times, which is what exercises the sprite path.
"""
import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

SHAPES = ["default", "text", "pointer", "crosshair", "ew-resize", "grab",
          "default", "text", "pointer", "wait"]


def main():
    want = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    app = Gtk.Application(application_id="com.tabdisplay.bench.cursors")

    def activate(app):
        win = Gtk.ApplicationWindow(application=app, title="tabdisplay cursors")
        box = Gtk.Box(homogeneous=True)
        for name in SHAPES:
            label = Gtk.Label(label=name, hexpand=True, vexpand=True)
            label.set_cursor(Gdk.Cursor.new_from_name(name))
            box.append(label)
        win.set_child(box)
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
