#!/usr/bin/env python3
"""Summarise tdcap's stdout: fps, bitrate, capture->packet time.

    tdcap ... | bench/readcap.py [dump.h264]
"""
import struct
import sys
import time

inp = sys.stdin.buffer
dump = open(sys.argv[1], "wb") if len(sys.argv) > 1 else None
n = size = 0
spent = []
other = {}
gaps = {}
prev = None
t0 = time.monotonic()
while True:
    head = inp.read(5)
    if len(head) < 5:
        break
    kind, ln = chr(head[0]), struct.unpack("<I", head[1:])[0]
    body = inp.read(ln)
    if kind == "V":
        ts, sp, flags = struct.unpack("<QIB", body[:13])
        n += 1
        if prev is not None:
            g = round((ts - prev) / 1000)
            gaps[g] = gaps.get(g, 0) + 1
        prev = ts
        size += ln - 13
        spent.append(sp / 1000)
        if dump:
            dump.write(body[13:])
        if flags & 1:
            print(f"keyframe {ln - 13} B", flush=True)
    else:
        other[kind] = other.get(kind, 0) + 1
    now = time.monotonic()
    if now - t0 >= 2 and n:
        spent.sort()
        print(f"{n / (now - t0):5.1f} fps  {size * 8 / (now - t0) / 1e6:6.2f} Mbit/s  "
              f"avg {size / n / 1024:6.1f} KiB  capture->packet ms: "
              f"p50 {spent[len(spent) // 2]:.1f} max {spent[-1]:.1f}  other {other}",
              flush=True)
        print("   gaps ms:", dict(sorted(gaps.items())), flush=True)
        n = size = 0
        spent = []
        other = {}
        gaps = {}
        t0 = now
