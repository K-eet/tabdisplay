#!/usr/bin/env python3
"""
Phase 2 smoke test: listen on TCP 7070 and push one solid-color RGBA frame
(default 2560x1600, red) to the first client that connects.

The Android receiver app connects to localhost:7070 over an `adb reverse
tcp:7070 tcp:7070` tunnel; on a successful end-to-end pipe the tablet's
screen turns the chosen colour and the script exits.

Frame protocol:
    4 bytes  big-endian uint32  payload length
    N bytes  raw RGBA pixels    (R, G, B, A repeated W*H times)
"""
import argparse
import socket
import struct
import sys
import time


def build_frame(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    pixel = bytes(rgba)
    return pixel * (width * height)


def serve_once(host: str, port: int, payload: bytes) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    print(f"[server] listening on {host}:{port} (frame {len(payload):,} bytes)")
    print(f"[hint] make sure 'adb reverse tcp:{port} tcp:{port}' is active")
    conn, addr = srv.accept()
    print(f"[server] client connected from {addr}")
    try:
        conn.settimeout(10.0)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        header = struct.pack(">I", len(payload))
        t0 = time.monotonic()
        conn.sendall(header)
        conn.sendall(payload)
        dt = time.monotonic() - t0
        mb = len(payload) / 1_000_000
        print(f"[server] sent {mb:.1f} MB in {dt*1000:.0f} ms "
              f"({mb / dt:.0f} MB/s) — tablet should now show the colour")
        time.sleep(2.0)
        return 0
    finally:
        try:
            conn.close()
        finally:
            srv.close()


COLORS = {
    "red":     (255,   0,   0, 255),
    "green":   (  0, 255,   0, 255),
    "blue":    (  0,   0, 255, 255),
    "white":   (255, 255, 255, 255),
    "black":   (  0,   0,   0, 255),
    "magenta": (255,   0, 255, 255),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7070)
    p.add_argument("--width", type=int, default=2560)
    p.add_argument("--height", type=int, default=1600)
    p.add_argument("--color", default="red", choices=sorted(COLORS))
    args = p.parse_args()

    payload = build_frame(args.width, args.height, COLORS[args.color])
    return serve_once(args.host, args.port, payload)


if __name__ == "__main__":
    sys.exit(main())
