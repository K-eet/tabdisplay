#!/usr/bin/env python3
"""tabdisplay host daemon.

    mutter virtual monitor --dmabuf--> tdcap (VAAPI) --pipe--> this daemon
        --TCP over `adb reverse`--> tablet

The daemon owns three things:

  * the link to the tablet: keeps `adb reverse` armed and the receiver app
    in the foreground for as long as the tablet is plugged in;
  * the virtual monitor: created when the app says hello (with the size the
    tablet reports), removed when the tablet goes away;
  * the stream: forwards tdcap's records to the app and reads its
    acknowledgements, which give a live capture-to-screen latency figure
    and bound how many frames may be in flight.

Wire protocol, both directions: [u8 type][u32 LE length][payload].

  tablet -> host
    'H'  "TABD", u16 version, u32 width, u32 height, u32 dpi, u32 codecs
    'A'  u64 capture timestamp of a frame now on screen, u32 us the tablet
         held it (arrival -> handed to the display)
    'T'  echo of a host 'T', payload unchanged
  host -> tablet
    'C'  u16 version, u32 width, u32 height, u8 codec (0 H.264, 1 HEVC)
    'V' 'P' 'S'  tdcap records, passed through unchanged (see tdcap.c)
    'T'  u64 host clock; echoed back, which measures the link round trip
"""
import argparse
import glob
import json
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time

from gi.repository import Gio, GLib

from scrub import Scrubber

VERSION = 3
HERE = os.path.dirname(os.path.abspath(__file__))
APP = "com.tabdisplay"
ACTIVITY = APP + "/.DisplayActivity"

SC = "org.gnome.Mutter.ScreenCast"
DC = "org.gnome.Mutter.DisplayConfig"
DC_PATH = "/org/gnome/Mutter/DisplayConfig"

log = logging.getLogger("tabdisplay")
DEBUG = bool(os.environ.get("TABDISPLAY_DEBUG"))


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return bytes(buf)


def read_msg(sock):
    head = recv_exact(sock, 5)
    kind, length = chr(head[0]), struct.unpack("<I", head[1:])[0]
    if length > 64 << 20:
        raise ConnectionError("oversized message")
    return kind, recv_exact(sock, length) if length else b""


def pack_msg(kind, payload=b""):
    return kind.encode() + struct.pack("<I", len(payload)) + payload


def parse_hello(payload):
    if len(payload) < 22 or payload[:4] != b"TABD":
        raise ConnectionError("not a tabdisplay client")
    version, width, height, dpi, codecs = struct.unpack("<HIIII", payload[4:22])
    if version != VERSION:
        raise ConnectionError(f"client speaks v{version}, daemon v{VERSION}")
    if not (128 <= width <= 4096 and 128 <= height <= 4096):
        raise ConnectionError(f"implausible size {width}x{height}")
    return width, height, dpi, codecs


# ---------------------------------------------------------------------------
# Monitor layout
# ---------------------------------------------------------------------------

class Layout:
    """Scale and position of the virtual monitor.

    mutter gives a new virtual monitor scale 1 at the right edge. The first
    time, only the scale is changed (2560x1600 on a 12" panel is unreadable
    at 1). After that, whatever arrangement the user makes in Settings is
    remembered per set of connected monitors and restored on the next plug.
    """

    def __init__(self, bus, path, default_scale):
        self.bus = bus
        self.path = path
        self.default_scale = default_scale
        self.applying = False
        self.settled = False    # set once the initial layout is in place
        self.virtual_rect = None    # logical x, y, w, h of our monitor

    def _state(self):
        return self.bus.call_sync(DC, DC_PATH, DC, "GetCurrentState", None,
                                  None, Gio.DBusCallFlags.NONE, 5000,
                                  None).unpack()

    def _note_rect(self, monitors, logical):
        sizes = {}
        for spec, mode_list, _props in monitors:
            for mode in mode_list:
                if mode[6].get("is-current"):
                    sizes[spec[0]] = (mode[1], mode[2])
        for lm in logical:
            conn = lm[5][0][0]
            if self._virtual(conn) and conn in sizes:
                w, h = sizes[conn]
                self.virtual_rect = (lm[0], lm[1], round(w / lm[2]),
                                     round(h / lm[2]))

    def refresh_rect(self):
        try:
            _serial, monitors, logical, _ = self._state()
        except GLib.Error:
            return
        self._note_rect(monitors, logical)

    @staticmethod
    def _virtual(connector):
        return connector.startswith("Meta-")

    def _load(self):
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _key(logical):
        return ",".join(sorted(m[0] for lm in logical for m in lm[5]
                               if not Layout._virtual(m[0])))

    def wait_and_apply(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            serial, monitors, logical, _ = self._state()
            if any(self._virtual(m[0]) for lm in logical for m in lm[5]):
                self._apply(serial, monitors, logical)
                return True
            time.sleep(0.1)
        log.warning("virtual monitor did not appear in the layout")
        return False

    def _apply(self, serial, monitors, logical):
        modes = {}          # connector -> (current mode id, supported scales)
        for spec, mode_list, _props in monitors:
            for mode in mode_list:
                if mode[6].get("is-current"):
                    modes[spec[0]] = (mode[0], mode[5])
        saved = self._load().get(self._key(logical))
        out = []
        for x, y, scale, transform, primary, mons, _props in logical:
            conn = mons[0][0]
            if self._virtual(conn):
                want = saved["scale"] if saved else self.default_scale
                scales = modes[conn][1] or [1.0]
                scale = min(scales, key=lambda s: abs(s - want))
            if saved and conn in saved["pos"] or (saved and self._virtual(conn)):
                px = saved["pos"].get("virtual" if self._virtual(conn) else conn)
                if px:
                    x, y = px
            out.append((x, y, scale, transform, primary,
                        [(m[0], modes[m[0]][0], {}) for m in mons]))
        self.applying = True
        try:
            self.bus.call_sync(
                DC, DC_PATH, DC, "ApplyMonitorsConfig",
                GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                             (serial, 1, out, {})),
                None, Gio.DBusCallFlags.NONE, 5000, None)
            log.info("layout applied: %s",
                     [(lm[5][0][0], lm[0], lm[1], round(lm[2], 3)) for lm in out])
        except GLib.Error as exc:
            log.warning("could not apply layout (%s)", exc.message)
            if saved:
                # A stale arrangement (e.g. overlapping) must not block the
                # scale from being applied next time.
                data = self._load()
                data.pop(self._key(logical), None)
                self._save(data)
        finally:
            self.applying = False
            self.settled = True
        self.refresh_rect()

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.path)

    def remember(self):
        """Record the current arrangement (called on MonitorsChanged)."""
        if self.applying or not self.settled:
            return
        try:
            _serial, monitors, logical, _ = self._state()
        except GLib.Error:
            return
        self._note_rect(monitors, logical)
        entry = {"pos": {}}
        for x, y, scale, _t, _p, mons, _props in logical:
            conn = mons[0][0]
            if self._virtual(conn):
                entry["scale"] = scale
                entry["pos"]["virtual"] = [x, y]
            else:
                entry["pos"][conn] = [x, y]
        if "scale" not in entry:
            return
        data = self._load()
        if data.get(self._key(logical)) != entry:
            data[self._key(logical)] = entry
            self._save(data)
            log.info("remembered layout: %s", entry)


# ---------------------------------------------------------------------------
# Virtual monitor + encoder
# ---------------------------------------------------------------------------

class Capture:
    """A mutter virtual monitor and the tdcap process encoding it."""

    def __init__(self, bus, args, width, height, layout, on_record, on_exit):
        self.bus = bus
        self.args = args
        self.size = (width, height)
        self.layout = layout
        self.on_record = on_record
        self.on_exit = on_exit
        self.session = None
        self.proc = None
        self.stopped = False
        self._cmd_lock = threading.Lock()

    def _call(self, path, iface, method, params=None, reply=None):
        return self.bus.call_sync(SC, path, iface, method, params,
                                  GLib.VariantType(reply) if reply else None,
                                  Gio.DBusCallFlags.NONE, 5000, None)

    def start(self):
        (self.session,) = self._call(
            "/org/gnome/Mutter/ScreenCast", SC, "CreateSession",
            GLib.Variant("(a{sv})", ({},)), "(o)")
        # cursor-mode 2 (overlay): the pointer is left out of the frames;
        # its position and image arrive as metadata and the tablet draws
        # it on top of the video, so pointer motion skips encode + decode.
        # cursor-mode 1 (embedded): the pointer is part of the picture.
        # is-platform: a monitor, not a "your screen is being shared" cast.
        (stream,) = self._call(
            self.session, SC + ".Session", "RecordVirtual",
            GLib.Variant("(a{sv})", ({
                "cursor-mode": GLib.Variant(
                    "u", 2 if self.args.cursor == "overlay" else 1),
                "is-platform": GLib.Variant("b", True),
            },)), "(o)")
        got_node = threading.Event()
        node = []

        def on_added(_c, _s, _p, _i, _sig, params):
            node.append(params[0])
            got_node.set()

        sub = self.bus.signal_subscribe(
            SC, SC + ".Stream", "PipeWireStreamAdded", stream, None,
            Gio.DBusSignalFlags.NONE, on_added)
        try:
            self._call(self.session, SC + ".Session", "Start")
            if not got_node.wait(5):
                raise RuntimeError("mutter did not provide a PipeWire node")
        finally:
            self.bus.signal_unsubscribe(sub)

        w, h = self.size
        cmd = [os.path.join(HERE, "tdcap"), "--node", str(node[0]),
               "--size", f"{w}x{h}", "--fps", str(self.args.fps),
               "--refresh", str(self.args.fps),
               "--codec", self.args.codec, "--rc", self.args.rc,
               "--qp", str(self.args.qp),
               "--bitrate", str(self.args.bitrate),
               "--maxrate", str(self.args.maxrate)]
        log.info("starting %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._pump, name="tdcap-out",
                         daemon=True).start()
        self.layout.wait_and_apply()

    def _pump(self):
        out = self.proc.stdout
        try:
            while True:
                head = out.read(5)
                if not head:
                    break
                while len(head) < 5:
                    more = out.read(5 - len(head))
                    if not more:
                        raise EOFError
                    head += more
                length = struct.unpack("<I", head[1:])[0]
                body = bytearray()
                while len(body) < length:
                    chunk = out.read(length - len(body))
                    if not chunk:
                        raise EOFError
                    body += chunk
                self.on_record(head, bytes(body))
        except (EOFError, OSError, ValueError):
            pass
        code = self.proc.wait()
        if not self.stopped:
            log.warning("tdcap exited with status %s", code)
            self.on_exit(self)

    def command(self, word):
        with self._cmd_lock:
            try:
                self.proc.stdin.write(word.encode() + b"\n")
            except (OSError, ValueError, AttributeError):
                pass

    def stop(self):
        self.stopped = True
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.session is not None:
            try:
                self._call(self.session, SC + ".Session", "Stop")
            except GLib.Error:
                pass        # session already gone (e.g. shell restarted)
            self.session = None


# ---------------------------------------------------------------------------
# Stream to one client
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.frames = self.bytes = 0
        self.latency = []
        self.held = []
        self.encode = []
        self.rtt = []
        self.relay = []
        self.t0 = time.monotonic()

    def report(self):
        with self.lock:
            dt = time.monotonic() - self.t0
            lat, held, enc = sorted(self.latency), self.held, self.encode
            rtt = self.rtt
            relay = self.relay
            frames, nbytes = self.frames, self.bytes
            self.reset()
        if not frames:
            return None
        if DEBUG and held:
            h = sorted(held)
            log.info("tablet ms: min %.1f p10 %.1f p50 %.1f p90 %.1f max %.1f",
                     *(h[min(len(h) - 1, int(len(h) * q))] / 1000
                       for q in (0, .1, .5, .9, 1)))

        def pct(v, p):
            return v[min(len(v) - 1, int(len(v) * p))] / 1000 if v else 0.0

        return ("%.1f fps, %.1f Mbit/s | capture->on-screen ms: p50 %.1f "
                "p95 %.1f max %.1f | encode %.1f, relay %.1f, tablet %.1f, link rtt %.1f" % (
                    frames / dt, nbytes * 8 / dt / 1e6, pct(lat, .5),
                    pct(lat, .95), pct(lat, 1.0),
                    sum(enc) / max(1, len(enc)) / 1000,
                    sum(relay) / max(1, len(relay)) / 1000,
                    sorted(held)[len(held) // 2] / 1000 if held else 0.0,
                    min(rtt) / 1000 if rtt else 0.0))


class Daemon:
    # Frames sent but not yet shown. Past this the sender stops reading
    # from the encoder, which makes tdcap drop frames *before* encoding:
    # a slow link costs frame rate, never latency.
    MAX_IN_FLIGHT = 4
    LINGER = 3.0        # keep the monitor this long after a client drops

    def __init__(self, args):
        self.args = args
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.layout = Layout(self.bus, args.layout_file, args.scale)
        self.lock = threading.RLock()
        self.capture = None
        self.client = None
        self.client_synced = False      # has it been sent a keyframe yet?
        self.sprite = None              # last cursor image, for late joiners
        self.pointer = None
        self.pointer_on = False
        self.scrubber = Scrubber()
        self.sent = self.acked = 0
        self.ack_cond = threading.Condition()
        self.send_lock = threading.Lock()
        self.linger = None
        self.stats = Stats()
        self.bus.signal_subscribe(DC, DC, "MonitorsChanged", DC_PATH, None,
                                  Gio.DBusSignalFlags.NONE,
                                  lambda *a: self._monitors_changed())

    def _monitors_changed(self):
        if self.capture is not None:
            threading.Thread(target=self.layout.remember, daemon=True).start()

    # -- records from tdcap (tdcap-out thread) ------------------------------

    def on_record(self, head, body):
        kind = head[:1]
        if kind == b"S":
            self.sprite = head + body
            if len(body) > 16:
                self._save_sprite(head + body)
            if DEBUG:
                log.info("sprite %s, %d bytes, %d non-transparent px",
                         struct.unpack_from("<IIii", body), len(body) - 16,
                         sum(1 for a in body[19::4] if a))
        elif kind == b"P":
            self.pointer = head + body
            on = bool(body[8])
            if DEBUG:
                log.info("pointer %s", struct.unpack("<iiB", body))
            if on != self.pointer_on:
                # Crossing onto or off the monitor is when mutter leaves
                # a stale pointer image behind (see scrub.py). Repaint
                # once the crossing has settled.
                self.pointer_on = on
                rect = self.layout.virtual_rect
                if rect:
                    self.scrubber.scrub(*rect, delay=0.12)
        client = self.client
        if client is None:
            return
        if kind == b"V":
            key = body[12] & 1
            if not self.client_synced:
                if not key:
                    return
                self.client_synced = True
            with self.ack_cond:
                deadline = time.monotonic() + 0.5
                while (self.sent - self.acked >= self.MAX_IN_FLIGHT
                       and self.client is client):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self.ack_cond.wait(left)
                self.sent += 1
            with self.stats.lock:
                self.stats.frames += 1
                self.stats.bytes += len(body)
                self.stats.encode.append(struct.unpack_from("<I", body, 8)[0])
        self.send(client, head + body)
        if kind == b"V":
            ts, spent = struct.unpack_from("<QI", body)
            with self.stats.lock:
                self.stats.relay.append(
                    time.monotonic_ns() // 1000 - ts - spent)

    def send(self, client, data):
        try:
            with self.send_lock:
                client.sendall(data)
        except OSError as exc:
            self.drop_client(client, f"send failed: {exc}")

    # mutter sends the pointer image only when the pointer's shape changes,
    # never when a stream starts. A tablet that has just connected would
    # have nothing to draw until then, so the last image seen is kept (on
    # disk too: the service starts afresh on every plug) and sent first.
    def _save_sprite(self, record):
        try:
            os.makedirs(os.path.dirname(self.args.sprite_file), exist_ok=True)
            tmp = self.args.sprite_file + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(record)
            os.replace(tmp, self.args.sprite_file)
        except OSError:
            pass

    def _load_sprite(self):
        try:
            with open(self.args.sprite_file, "rb") as fh:
                record = fh.read()
        except OSError:
            return None
        if (len(record) > 21 and record[:1] == b"S" and
                struct.unpack_from("<I", record, 1)[0] == len(record) - 5):
            return record
        return None

    def on_capture_exit(self, capture):
        with self.lock:
            if self.capture is capture:
                self.teardown("encoder exited")

    # -- client handling ----------------------------------------------------

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.bind, self.args.port))
        srv.listen(2)
        log.info("listening on %s:%d", self.args.bind, self.args.port)
        while True:
            conn, _addr = srv.accept()
            threading.Thread(target=self.handle_client, args=(conn,),
                             name="client", daemon=True).start()

    def handle_client(self, conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # A small send buffer keeps queued-but-unsent video short; the
        # in-flight limit does the rest.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
        try:
            conn.settimeout(5)
            kind, payload = read_msg(conn)
            if kind != "H":
                raise ConnectionError("expected hello")
            width, height, dpi, codecs = parse_hello(payload)
            conn.settimeout(None)
        except (OSError, ConnectionError, struct.error) as exc:
            log.warning("rejected connection: %s", exc)
            conn.close()
            return
        log.info("tablet connected: %dx%d, %d dpi", width, height, dpi)
        if self.args.size:
            width, height = self.args.size
        if self.args.codec == "hevc" and not codecs & 2:
            log.warning("tablet did not offer HEVC; it may fail to decode")

        with self.lock:
            if self.linger is not None:
                self.linger.cancel()
                self.linger = None
            if self.client is not None:
                self.drop_client(self.client, "replaced by a new connection")
            if self.capture is not None and self.capture.size != (width, height):
                self.teardown("tablet size changed")
            try:
                conn.sendall(pack_msg("C", struct.pack(
                    "<HIIB", VERSION, width, height,
                    1 if self.args.codec == "hevc" else 0)))
                # An empty image means "hidden" and is not worth replaying.
                if self.sprite is None or len(self.sprite) <= 21:
                    self.sprite = self._load_sprite()
                for cached in (self.sprite, self.pointer):
                    if cached:
                        conn.sendall(cached)
                with self.ack_cond:
                    self.sent = self.acked = 0
                self.client_synced = False
                self.client = conn
                if self.capture is None:
                    self.pointer = None
                    self.layout.settled = False
                    self.capture = Capture(self.bus, self.args, width, height,
                                           self.layout, self.on_record,
                                           self.on_capture_exit)
                    self.capture.start()
                else:
                    self.capture.command("idr")
            except Exception as exc:
                log.error("could not start the display: %s", exc)
                self.client = None
                conn.close()
                self.teardown("startup failed")
                return

        try:
            while True:
                kind, payload = read_msg(conn)
                if kind == "A" and len(payload) >= 12:
                    ts, held = struct.unpack_from("<QI", payload)
                    now = time.monotonic_ns() // 1000
                    with self.stats.lock:
                        self.stats.latency.append(now - ts)
                        self.stats.held.append(held)
                    with self.ack_cond:
                        self.acked += 1
                        self.ack_cond.notify_all()
                elif kind == "T" and len(payload) >= 8:
                    sent_at = struct.unpack_from("<Q", payload)[0]
                    with self.stats.lock:
                        self.stats.rtt.append(
                            time.monotonic_ns() // 1000 - sent_at)
        except (OSError, ConnectionError):
            pass
        self.drop_client(conn, "connection closed")

    def drop_client(self, conn, why):
        with self.lock:
            if self.client is not conn:
                try:
                    conn.close()
                except OSError:
                    pass
                return
            log.info("tablet disconnected (%s)", why)
            self.client = None
            with self.ack_cond:
                self.ack_cond.notify_all()
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            # The app reconnects within a second after a hiccup; removing
            # the monitor at once would throw every window back to the
            # laptop for nothing.
            self.linger = threading.Timer(self.LINGER, self._linger_expired)
            self.linger.daemon = True
            self.linger.start()

    def _linger_expired(self):
        with self.lock:
            if self.client is None:
                self.teardown("no client")

    def teardown(self, why):
        with self.lock:
            if self.linger is not None:
                self.linger.cancel()
                self.linger = None
            capture, self.capture = self.capture, None
            if capture is not None:
                log.info("removing the virtual monitor (%s)", why)
                capture.stop()
            client, self.client = self.client, None
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass

    # -- housekeeping -------------------------------------------------------

    def stats_loop(self):
        tick = 0
        while True:
            time.sleep(1)
            client = self.client
            if client is not None:
                self.send(client, pack_msg("T", struct.pack(
                    "<Q", time.monotonic_ns() // 1000)))
            tick += 1
            if tick % 5:
                continue
            line = self.stats.report()
            if line:
                with self.ack_cond:
                    flight = self.sent - self.acked
                log.info("%s | in flight %d", line, flight)
                capture = self.capture
                if capture is not None:
                    capture.command("stat")

    def idle_loop(self):
        """Ask for one more frame shortly after the screen stops changing.

        Capture is damage-driven: when nothing changes, nothing is sent.
        Under rate control the last frame of a burst can be the worst one,
        and it would then sit on screen indefinitely; one or two repeats
        let the encoder spend its idle budget on sharpening it. Pointless
        for constant-QP encoding, so only done otherwise.
        """
        if self.args.rc == "cqp":
            return
        last_frames, quiet, topped = -1, 0, 0
        while True:
            time.sleep(0.1)
            with self.ack_cond:
                sent = self.sent
            if sent != last_frames and topped == 0:
                last_frames, quiet = sent, 0
                continue
            quiet += 1
            if self.capture is None or self.client is None:
                topped = 0
                continue
            if sent != last_frames:         # our own top-up frame arrived
                last_frames = sent
                if topped >= 3:
                    topped, quiet = 0, -10**9   # done until real motion
                    continue
            if quiet >= 2 and topped < 3:
                self.capture.command("refine")
                topped += 1
                quiet = 0


# ---------------------------------------------------------------------------
# adb: keep the tablet wired up while it is plugged in
# ---------------------------------------------------------------------------

class Adb:
    def __init__(self, args, daemon):
        self.args = args
        self.daemon = daemon

    def _adb(self, *cmd, timeout=10):
        base = ["adb"]
        if self.args.serial:
            base += ["-s", self.args.serial]
        return subprocess.run(base + list(cmd), capture_output=True,
                              text=True, timeout=timeout)

    def _on_usb(self):
        """Is the tablet on the bus at all? Costs a few file reads, where
        asking adb costs a process; unknown serial means "assume so"."""
        if not self.args.serial:
            return True
        for path in glob.glob("/sys/bus/usb/devices/*/serial"):
            try:
                with open(path) as fh:
                    if fh.read().strip() == self.args.serial:
                        return True
            except OSError:
                pass
        return False

    def _state(self):
        if not self._on_usb():
            return "absent"
        try:
            return self._adb("get-state", timeout=5).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""

    def bring_up(self):
        port = str(self.args.port)
        if self._adb("reverse", "tcp:7070", "tcp:" + port).returncode != 0:
            return False
        # Wake the screen (a no-op when it is already on), then put the
        # receiver in front. FLAG_SHOW_WHEN_LOCKED in the app lets it draw
        # over a lock screen, as a monitor would.
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        r = self._adb("shell", "am", "start", "-n", ACTIVITY)
        if r.returncode != 0 or "Error" in r.stdout + r.stderr:
            log.warning("could not start the app: %s",
                        (r.stdout + r.stderr).strip())
            return False
        log.info("tablet is up: adb reverse armed, app started")
        return True

    def loop(self):
        up = False
        fails = 0
        while True:
            state = self._state()
            if state == "device":
                fails = 0
                if not up:
                    up = self.bring_up()
                elif self._adb("reverse", "--list").stdout.strip() == "":
                    up = False          # adbd restarted: rules are gone
                    continue
            else:
                if up:
                    log.info("tablet unplugged")
                    self.daemon.teardown("tablet unplugged")
                up = False
                fails = fails + 1 if state != "absent" else 0
                if fails == 8:
                    # A device that enumerates on USB but never reaches
                    # "device" is usually a wedged adb server.
                    log.warning("adb cannot see the tablet (state %r); "
                                "restarting the adb server", state)
                    subprocess.run(["adb", "kill-server"],
                                   capture_output=True)
            time.sleep(1.0 if up else 0.5)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=7070)
    p.add_argument("--bind", default="127.0.0.1",
                   help="listen address; loopback is all adb reverse needs "
                        "and the stream is unauthenticated")
    p.add_argument("--serial", default=os.environ.get("ADB_SERIAL"),
                   help="adb serial of the tablet (default: the only device)")
    p.add_argument("--no-adb", action="store_true",
                   help="do not manage adb reverse / app launch")
    p.add_argument("--codec", choices=["h264", "hevc"], default="h264")
    p.add_argument("--cursor", choices=["overlay", "embedded"],
                   default="overlay",
                   help="overlay: the tablet draws the pointer itself, so it "
                        "tracks the mouse without video latency. embedded: "
                        "the pointer is part of the video.")
    p.add_argument("--rc", choices=["cqp", "vbr", "cbr"], default="cqp")
    p.add_argument("--qp", type=int, default=24)
    p.add_argument("--bitrate", type=int, default=20000, help="kbit/s")
    p.add_argument("--maxrate", type=int, default=40000, help="kbit/s")
    p.add_argument("--fps", type=int, default=60,
                   help="refresh rate of the virtual monitor and of the "
                        "stream; match the tablet's panel")
    p.add_argument("--size", type=lambda v: tuple(int(n) for n in v.split("x")),
                   help="WxH of the virtual monitor instead of the tablet's "
                        "native size; the tablet scales it to fit")
    p.add_argument("--scale", type=float, default=1.75,
                   help="display scale used until one is set in Settings")
    p.add_argument("--sprite-file", default=os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
        "tabdisplay", "pointer.bin"), help=argparse.SUPPRESS)
    p.add_argument("--layout-file", default=os.path.join(
        os.environ.get("XDG_CONFIG_HOME",
                       os.path.expanduser("~/.config")),
        "tabdisplay", "layout.json"))
    return p


def main():
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s")
    daemon = Daemon(args)
    for target in (daemon.serve, daemon.stats_loop, daemon.idle_loop):
        threading.Thread(target=target, daemon=True).start()
    if not args.no_adb:
        threading.Thread(target=Adb(args, daemon).loop, daemon=True).start()

    loop = GLib.MainLoop()

    def quit_(*_):
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, quit_)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, quit_)
    try:
        loop.run()
    finally:
        daemon.teardown("daemon stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
