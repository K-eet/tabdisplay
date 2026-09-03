#!/usr/bin/env python3
"""
Phase 3 daemon: bridge evdi framebuffer -> TCP socket -> tablet receiver.

- Owns one persistent TCP server on 7070.
- Spawns one virtual DRM card via libevdi/PyEvdi and connects an EDID so
  mutter enumerates a 2560x1600 monitor.
- On every evdi `acquire_framebuffer` callback, swaps the XRGB8888 little-endian
  source bytes (B G R X in memory) into RGBA, slices the framebuffer by
  pyevdi's dirty rects, and sends a multi-rect packet to the tablet.

Protocol (host -> tablet, big-endian):
    [u32] num_rects
    repeat num_rects times:
        [u32] x1
        [u32] y1
        [u32] x2
        [u32] y2
        [u32] payload_length (bytes)
        [N]   RGBA pixel data for that rect, row-major (x1..x2, y1..y2)

If pyevdi reports no rects or the dirty area exceeds 80% of the frame, the
daemon falls back to one full-frame rect (0,0,W,H).
"""
import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

import PyEvdi


FRAME_W = 2560
FRAME_H = 1600
PORT = 7070

DEFAULT_EDID = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "edid_2560x1600.bin")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
)
log = logging.getLogger("tabdisplay")


class FrameSink:
    """Persistent TCP listener on ``port``; holds at most one client at a
    time and exposes :meth:`send_frame` to push packets to it."""

    def __init__(self, port: int):
        self.port = port
        self._lock = threading.Lock()
        self._conn = None
        self._dead = threading.Event()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="acceptor",
                                        daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(1)
        srv.settimeout(1.0)
        log.info("listening on 0.0.0.0:%d", self.port)
        while not self._shutdown.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log.info("client connected from %s", addr)
            with self._lock:
                self._conn = conn
                self._dead.clear()
            self._dead.wait()
            with self._lock:
                old, self._conn = self._conn, None
            try:
                if old is not None:
                    old.close()
            except OSError:
                pass
            log.info("client disconnected; re-accepting")
        srv.close()

    def send_frame(self, packet: bytes) -> bool:
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.sendall(packet)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.warning("send failed (%s) — dropping connection", exc)
            self._dead.set()
            return False

    def has_client(self) -> bool:
        with self._lock:
            return self._conn is not None

    def shutdown(self) -> None:
        self._shutdown.set()
        self._dead.set()


def xrgb_to_rgba(view: memoryview, width: int, height: int) -> np.ndarray:
    """Convert evdi's XRGB8888 little-endian framebuffer (memory order B G R X)
    into an HxWx4 RGBA uint8 array. Forces alpha to 255."""
    src = np.frombuffer(view, dtype=np.uint8).reshape(height, width, 4)
    out = np.empty_like(src)
    out[..., 0] = src[..., 2]   # R from byte 2 (R-position in XRGB)
    out[..., 1] = src[..., 1]   # G
    out[..., 2] = src[..., 0]   # B from byte 0 (B-position)
    out[..., 3] = 255           # A (source X is undefined)
    return out


def normalize_rects(raw_rects, width: int, height: int,
                    full_threshold: float = 0.8):
    """Clip each evdi_rect to the frame, drop empties, and collapse to a
    single full-frame rect if either no rects survived or the total dirty
    area exceeds ``full_threshold`` of the screen."""
    out = []
    total = 0
    for r in raw_rects:
        x1 = max(0, min(width,  r.x1))
        x2 = max(0, min(width,  r.x2))
        y1 = max(0, min(height, r.y1))
        y2 = max(0, min(height, r.y2))
        if x2 > x1 and y2 > y1:
            out.append((x1, y1, x2, y2))
            total += (x2 - x1) * (y2 - y1)
    if not out or total > full_threshold * width * height:
        return [(0, 0, width, height)]
    return out


def encode_packet(rgba: np.ndarray, rects) -> bytes:
    """Pack the multi-rect protocol packet for one frame."""
    parts = [struct.pack(">I", len(rects))]
    for (x1, y1, x2, y2) in rects:
        sub = np.ascontiguousarray(rgba[y1:y2, x1:x2, :]).tobytes()
        parts.append(struct.pack(">IIIII", x1, y1, x2, y2, len(sub)))
        parts.append(sub)
    return b"".join(parts)


# ---------------------------------------------------------------------------
# evdi mode
# ---------------------------------------------------------------------------

def pick_evdi_card() -> int:
    for i in range(20):
        if PyEvdi.check_device(i) == PyEvdi.AVAILABLE:
            return i
    PyEvdi.add_device()
    for i in range(20):
        if PyEvdi.check_device(i) == PyEvdi.AVAILABLE:
            return i
    return -1


def run_evdi(sink: FrameSink, edid_path: str) -> int:
    idx = pick_evdi_card()
    if idx < 0:
        log.error("no AVAILABLE evdi card; is the kernel module loaded?")
        return 2

    with open(edid_path, "rb") as fh:
        edid = fh.read()
    log.info("loaded EDID %s (%d bytes)", edid_path, len(edid))

    card = PyEvdi.Card(idx)
    log.info("opened evdi card %d", idx)

    stats = {"frames": 0, "last_log": time.monotonic(), "bytes": 0}

    def on_mode_changed(mode):
        log.info("mode_changed: %dx%d @ %d Hz, bpp=%d, pixel_format=0x%x",
                 mode.width, mode.height, mode.refresh_rate,
                 mode.bits_per_pixel, mode.pixel_format)

    def on_acquire_framebuffer(buf):
        if not sink.has_client():
            return
        try:
            rgba = xrgb_to_rgba(buf.bytes, buf.width, buf.height)
        except Exception:
            log.exception("xrgb->rgba conversion failed")
            return
        rects = normalize_rects(buf.rects, buf.width, buf.height)
        packet = encode_packet(rgba, rects)
        if sink.send_frame(packet):
            stats["frames"] += 1
            stats["bytes"] += len(packet)
            now = time.monotonic()
            if now - stats["last_log"] >= 5.0:
                dt = now - stats["last_log"]
                fps = stats["frames"] / dt
                mb = stats["bytes"] / 1_000_000
                log.info("sent %d frames in %.1fs (%.1f fps, %.1f MB total)",
                         stats["frames"], dt, fps, mb)
                stats.update(frames=0, bytes=0, last_log=now)

    card.mode_changed_handler = on_mode_changed
    card.acquire_framebuffer_handler = on_acquire_framebuffer

    # These thresholds bound the EDID modes mutter will accept; we set them
    # generously above 2560x1600@60 so our preferred timing isn't rejected.
    sku_area = FRAME_W * FRAME_H
    sku_pclk = sku_area * 60
    card.connect(edid, len(edid), sku_area, sku_pclk)
    log.info("evdi connected; pumping events (Ctrl-C to stop)")

    try:
        while True:
            card.handle_events(500)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        try:
            card.disconnect()
        except Exception:
            log.exception("disconnect failed")
        try:
            card.close()
        except Exception:
            log.exception("close failed")
    return 0


# ---------------------------------------------------------------------------
# Test pattern (bypasses evdi entirely)
# ---------------------------------------------------------------------------

def run_test_pattern(sink: FrameSink, period_s: float) -> int:
    """Cycle through known colours.

    Crucially, this routes through ``xrgb_to_rgba`` so the swap function is
    actually exercised. We build the source frame in XRGB8888 little-endian
    (memory order B G R X) — the exact layout pyevdi/mutter delivers — and
    let the daemon's normal pipeline produce the RGBA bytes the tablet
    expects. If the swap is broken the tablet shows the wrong colour.
    """
    palette = [
        ("red",     (255,   0,   0)),
        ("green",   (  0, 255,   0)),
        ("blue",    (  0,   0, 255)),
        ("white",   (255, 255, 255)),
        ("magenta", (255,   0, 255)),
    ]
    log.info("test-pattern: cycling %d colours every %.1fs (via xrgb_to_rgba)",
             len(palette), period_s)
    i = 0
    try:
        while True:
            name, (r, g, b) = palette[i % len(palette)]
            # XRGB8888 LE: each pixel is the 32-bit int (0xXXRRGGBB), which
            # in little-endian memory is the byte sequence (B, G, R, X).
            src = np.empty((FRAME_H, FRAME_W, 4), dtype=np.uint8)
            src[..., 0] = b  # byte 0 = B
            src[..., 1] = g  # byte 1 = G
            src[..., 2] = r  # byte 2 = R
            src[..., 3] = 0  # byte 3 = X (unused)
            rgba = xrgb_to_rgba(memoryview(src), FRAME_W, FRAME_H)
            packet = encode_packet(rgba, [(0, 0, FRAME_W, FRAME_H)])
            ok = sink.send_frame(packet)
            log.info("[%d] %-7s xrgb-in=(B=%d,G=%d,R=%d) rgba-out[0,0]=%s sent=%s",
                     i, name, b, g, r, tuple(int(x) for x in rgba[0, 0]), ok)
            i += 1
            time.sleep(period_s)
    except KeyboardInterrupt:
        log.info("test-pattern stopping")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--edid", default=DEFAULT_EDID,
                   help="path to 128-byte EDID for the virtual display")
    p.add_argument("--test-pattern", action="store_true",
                   help="bypass evdi; push a colour cycle to validate the wire")
    p.add_argument("--test-period", type=float, default=1.0,
                   help="seconds per colour in --test-pattern mode")
    args = p.parse_args()

    sink = FrameSink(args.port)
    try:
        if args.test_pattern:
            return run_test_pattern(sink, args.test_period)
        return run_evdi(sink, args.edid)
    finally:
        sink.shutdown()


if __name__ == "__main__":
    sys.exit(main())
