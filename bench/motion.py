#!/usr/bin/env python3
"""Full-screen motion load for a chosen monitor.

    bench/motion.py <text matched against connector/model> [seconds]

Draws vertically scrolling bars and text (GPU-rendered, so the load lands
on the stream, not on this process) and prints the frame rate the
compositor actually gave it. Used to compare pipelines under identical
content.
"""
import sys
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Graphene, Gtk, Pango  # noqa: E402


class Scroller(Gtk.Widget):
    def __init__(self):
        super().__init__()
        self.t0 = time.monotonic()
        self.frames = 0
        self.last = self.t0
        self.add_tick_callback(self._tick)
        self.layout = self.create_pango_layout("")
        self.layout.set_font_description(Pango.FontDescription("Monospace 14"))

    def _tick(self, *_):
        self.frames += 1
        now = time.monotonic()
        if now - self.last >= 2.0:
            print(f"motion: {self.frames / (now - self.last):.1f} fps",
                  flush=True)
            self.frames, self.last = 0, now
        self.queue_draw()
        return True

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        t = time.monotonic() - self.t0
        off = (t * 600.0) % 80.0            # 600 px/s scroll
        white = Gdk.RGBA(); white.parse("#f4f4f4")
        snap.append_color(white, Graphene.Rect().init(0, 0, w, h))
        dark = Gdk.RGBA(); dark.parse("#202428")
        blue = Gdk.RGBA(); blue.parse("#3465a4")
        y, i = -off, int(t * 600.0 // 80.0)
        while y < h:
            snap.append_color(blue if i % 5 == 0 else dark,
                              Graphene.Rect().init(40, y + 30, w - 80, 2))
            self.layout.set_text(
                f"line {i:06d}  the quick brown fox jumps over the lazy dog "
                f"0123456789 {{}}[]()<>", -1)
            snap.save()
            snap.translate(Graphene.Point().init(40, y))
            snap.append_layout(self.layout, dark)
            snap.restore()
            y += 80
            i += 1


def main():
    want = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    app = Gtk.Application(application_id="com.tabdisplay.bench.motion")

    def activate(app):
        win = Gtk.ApplicationWindow(application=app, title="tabdisplay motion")
        win.set_child(Scroller())
        target = None
        mons = Gdk.Display.get_default().get_monitors()
        for m in mons:
            desc = f"{m.get_connector()} {m.get_model()} {m.get_manufacturer()}"
            print("monitor:", desc, flush=True)
            if want and want in desc.lower():
                target = m
        if target is None:
            print("no monitor matched", repr(want), file=sys.stderr)
            app.quit()
            return
        win.fullscreen_on_monitor(target)
        win.present()
        if secs:
            GLib.timeout_add(int(secs * 1000), app.quit)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
