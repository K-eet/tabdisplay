#!/usr/bin/env python3
"""Hold a mutter virtual monitor open and print its PipeWire node id.

    bench/vmon.py [seconds]

Probe tool: lets a gst-launch pipeline be tried against the stream by hand.
"""
import sys

import gi
from gi.repository import Gio, GLib

BUS = "org.gnome.Mutter.ScreenCast"


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)

    def call(path, iface, method, args, reply=None):
        return bus.call_sync(BUS, path, iface, method, args,
                             GLib.VariantType(reply) if reply else None,
                             Gio.DBusCallFlags.NONE, -1, None)

    (session,) = call("/org/gnome/Mutter/ScreenCast", BUS, "CreateSession",
                      GLib.Variant("(a{sv})", ({},)), "(o)")
    (stream,) = call(session, BUS + ".Session", "RecordVirtual",
                     GLib.Variant("(a{sv})", ({
                         "cursor-mode": GLib.Variant("u", int(__import__("os").environ.get("CURSOR_MODE", "1"))),
                         "is-platform": GLib.Variant("b", True),
                     },)), "(o)")
    loop = GLib.MainLoop()

    def on_added(_c, _s, _p, _i, _sig, params):
        print("node", params[0], flush=True)

    bus.signal_subscribe(BUS, BUS + ".Stream", "PipeWireStreamAdded", stream,
                         None, Gio.DBusSignalFlags.NONE, on_added)
    call(session, BUS + ".Session", "Start", None)
    GLib.timeout_add(int(secs * 1000), loop.quit)
    loop.run()
    call(session, BUS + ".Session", "Stop", None)


if __name__ == "__main__":
    main()
