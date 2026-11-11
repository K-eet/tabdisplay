#!/usr/bin/env python3
"""Stand in for the tablet: say hello, acknowledge every frame at once.

    bench/fakeclient.py [seconds] [WxH] [port] [input]

With a fourth argument it also plays a short input script three seconds
in: a finger tap at (0.25, 0.25), a two-finger touch, then a pen stroke
from (0.5, 0.5) to (0.7, 0.6) with rising pressure, then a short eraser stroke.

Exercises the daemon end to end (virtual monitor, encoder, flow control)
with no tablet attached. Prints what it received.
"""
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))
import tabdisplayd as td  # noqa: E402

secs = float(sys.argv[1]) if len(sys.argv) > 1 else 5
w, h = (int(v) for v in (sys.argv[2] if len(sys.argv) > 2 else "2560x1600").split("x"))
port = int(sys.argv[3]) if len(sys.argv) > 3 else 7070

s = socket.create_connection(("127.0.0.1", port))
s.sendall(td.pack_msg("H", b"TABD" + struct.pack("<HIIII", td.VERSION, w, h, 244, 3)))
play_input = len(sys.argv) > 4


def u(v):
    return round(v * 65535)


def fingers(*pts):
    return td.pack_msg("F", bytes([len(pts)]) + b"".join(
        struct.pack("<BBHH", i, down, u(x), u(y)) for i, down, x, y in pts))


def pen(flags, x, y, pressure=0.0, tx=0, ty=0):
    return td.pack_msg("N", struct.pack("<BHHHbb", flags, u(x), u(y),
                                        u(pressure), tx, ty))


def input_script():
    yield fingers((0, 1, .25, .25))
    yield fingers((0, 0, .25, .25))
    yield fingers((0, 1, .30, .40), (1, 1, .60, .40))
    yield fingers((0, 1, .32, .42), (1, 1, .58, .42))
    yield fingers((0, 0, .32, .42), (1, 0, .58, .42))
    yield pen(1, .5, .5)
    for i in range(11):
        yield pen(3, .5 + .02 * i, .5 + .01 * i, .1 + .08 * i, 20, -10)
    yield pen(1, .7, .6)
    yield pen(0, .7, .6)
    yield pen(1 | 8, .4, .4)
    yield pen(1 | 2 | 8, .41, .4, .5)
    yield pen(1 | 2 | 8, .43, .4, .5)
    yield pen(1 | 8, .43, .4)
    yield pen(0, .4, .4)


script = input_script() if play_input else iter(())
script_at = time.monotonic() + 3
counts, nbytes, first_key = {}, 0, None
end = time.monotonic() + secs
s.settimeout(0.05)
while time.monotonic() < end:
    if time.monotonic() >= script_at:
        step = next(script, None)
        if step is not None:
            s.sendall(step)
            script_at = time.monotonic() + 0.06
    try:
        kind, body = td.read_msg(s)
    except socket.timeout:
        continue
    counts[kind] = counts.get(kind, 0) + 1
    if kind == "V":
        if first_key is None:
            first_key = bool(body[12] & 1)
        nbytes += len(body)
        s.sendall(td.pack_msg("A", body[:8] + struct.pack("<I", 0)))
    elif kind == "T":
        s.sendall(td.pack_msg("T", body))
print("messages:", dict(sorted(counts.items())), "| video %.1f KiB" % (nbytes / 1024),
      "| first frame is a keyframe:", first_key)
