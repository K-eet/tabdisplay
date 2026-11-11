#!/usr/bin/env python3
"""Stand in for the tablet: say hello, acknowledge every frame at once.

    bench/fakeclient.py [seconds] [WxH] [port]

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
counts, nbytes, first_key = {}, 0, None
end = time.monotonic() + secs
s.settimeout(0.5)
while time.monotonic() < end:
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
