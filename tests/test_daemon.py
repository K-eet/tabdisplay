#!/usr/bin/env python3
"""Sender unit tests. Run from the repo root:

    python3 -m unittest discover -s tests -v

No evdi device, tablet or adb needed. The one test that drives the real
h264_vaapi encoder is skipped when ffmpeg or the render node is missing.
"""
import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evdi-src", "pyevdi"))
try:
    import PyEvdi  # noqa: F401
except ImportError:                      # pyevdi not built: nothing here uses it
    sys.modules["PyEvdi"] = types.ModuleType("PyEvdi")

import daemon_v2 as d  # noqa: E402

logging.disable(logging.CRITICAL)      # keep test output to the results


def wait_until(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def recv_exact(sock, n, timeout=3.0):
    sock.settimeout(timeout)
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


def default_args(*argv):
    return d.build_parser().parse_args(list(argv))


# ---------------------------------------------------------------------------

class MotionDetectorTest(unittest.TestCase):
    def test_enters_video_after_n_high_consecutive_frames(self):
        det = d.MotionDetector()
        results = [det.update(0.95) for _ in range(det.N_HIGH)]
        self.assertEqual(results[:-1], [None] * (det.N_HIGH - 1))
        self.assertEqual(results[-1], "video")

    def test_middle_band_frame_breaks_a_high_streak(self):
        det = d.MotionDetector()
        for _ in range(det.N_HIGH - 1):
            det.update(0.95)
        det.update(0.60)
        self.assertIsNone(det.update(0.95))

    def test_returns_to_desktop_after_n_low_consecutive_frames(self):
        det = d.MotionDetector()
        results = [det.update(0.05) for _ in range(det.N_LOW)]
        self.assertEqual(results[-1], "desktop")
        self.assertNotIn("desktop", results[:-1])

    def test_custom_thresholds_are_honoured(self):
        det = d.MotionDetector(threshold_high=0.5)
        results = [det.update(0.6) for _ in range(det.N_HIGH)]
        self.assertEqual(results[-1], "video")


class WarmupGateTest(unittest.TestCase):
    def test_blocks_busy_frames_until_one_quiet_frame(self):
        gate = d.WarmupGate()
        self.assertFalse(gate.allow(1.0, 0.4))
        self.assertFalse(gate.allow(1.0, 0.4))
        self.assertTrue(gate.allow(0.1, 0.4))
        self.assertTrue(gate.allow(1.0, 0.4))

    def test_opens_after_frame_cap_when_screen_stays_busy(self):
        gate = d.WarmupGate()
        results = [gate.allow(1.0, 0.4) for _ in range(gate.MAX_FRAMES)]
        self.assertFalse(any(results[:-1]))
        self.assertTrue(results[-1])


# ---------------------------------------------------------------------------

class ReadAccessUnitsTest(unittest.TestCase):
    """read_access_units pairs ffmpeg's byte stream with its per-packet
    size report so each AU is known complete the moment its last byte
    arrives."""

    def setUp(self):
        self.data_r, self.data_w = os.pipe()
        self.sizes_r, self.sizes_w = os.pipe()

    def tearDown(self):
        for fd in (self.data_r, self.data_w, self.sizes_r, self.sizes_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def _close_writers(self):
        os.close(self.data_w)
        os.close(self.sizes_w)

    def test_splits_stream_at_reported_sizes(self):
        os.write(self.sizes_w, b"0, 0, 0, 1, 3, 0xaa\n0, 1, 1, 1, 5, 0xbb, F=0x0\n")
        os.write(self.data_w, b"abcDEFGH")
        self._close_writers()
        self.assertEqual(list(d.read_access_units(self.data_r, self.sizes_r)),
                         [b"abc", b"DEFGH"])

    def test_ignores_header_comment_lines(self):
        os.write(self.sizes_w, b"#software: Lavf\n#tb 0: 1/60\n0, 0, 0, 1, 2, 0x0\n")
        os.write(self.data_w, b"xy")
        self._close_writers()
        self.assertEqual(list(d.read_access_units(self.data_r, self.sizes_r)),
                         [b"xy"])

    def test_yields_au_before_any_later_bytes_exist(self):
        # The whole point: no waiting for the next AU to delimit this one.
        gen = d.read_access_units(self.data_r, self.sizes_r)
        os.write(self.sizes_w, b"0, 0, 0, 1, 4, 0x0\n")
        os.write(self.data_w, b"only")
        got = []
        t = threading.Thread(target=lambda: got.append(next(gen)), daemon=True)
        t.start()
        t.join(timeout=2)
        self.assertEqual(got, [b"only"])
        self._close_writers()

    def test_handles_size_line_split_across_reads_and_late_data(self):
        gen = d.read_access_units(self.data_r, self.sizes_r)
        got = []
        t = threading.Thread(target=lambda: got.extend(gen), daemon=True)
        t.start()
        os.write(self.data_w, b"he")
        os.write(self.sizes_w, b"0, 0, 0, 1,")
        time.sleep(0.05)
        os.write(self.sizes_w, b" 5, 0x0\n")
        time.sleep(0.05)
        os.write(self.data_w, b"llo")
        self._close_writers()
        t.join(timeout=2)
        self.assertEqual(got, [b"hello"])

    def test_drops_incomplete_trailing_au_at_eof(self):
        os.write(self.sizes_w, b"0, 0, 0, 1, 2, 0x0\n0, 1, 1, 1, 9, 0x0\n")
        os.write(self.data_w, b"okpart")
        self._close_writers()
        self.assertEqual(list(d.read_access_units(self.data_r, self.sizes_r)),
                         [b"ok"])


class FrameAuTest(unittest.TestCase):
    def test_prefixes_big_endian_length(self):
        self.assertEqual(d.frame_au(b"\x00\x00\x00\x01\x09"),
                         b"\x00\x00\x00\x05\x00\x00\x00\x01\x09")


# ---------------------------------------------------------------------------

class FrameSinkTest(unittest.TestCase):
    def setUp(self):
        self.socks = []
        self.sink = None

    def tearDown(self):
        for s in self.socks:
            s.close()
        if self.sink is not None:
            self.sink.shutdown()

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.sink.port), timeout=2)
        self.socks.append(s)
        return s

    def test_listens_on_loopback_only_by_default(self):
        self.sink = d.FrameSink(0)
        self.assertEqual(self.sink.host, "127.0.0.1")
        self.assertNotEqual(self.sink.port, 0)

    def test_bind_failure_raises_in_constructor(self):
        self.sink = d.FrameSink(0)
        with self.assertRaises(OSError):
            d.FrameSink(self.sink.port)

    def test_delivers_data_to_connected_client(self):
        self.sink = d.FrameSink(0)
        c = self._connect()
        self.assertTrue(wait_until(self.sink.has_client))
        self.assertTrue(self.sink.send(b"hello"))
        self.assertEqual(recv_exact(c, 5), b"hello")

    def test_send_without_client_returns_false(self):
        self.sink = d.FrameSink(0)
        self.assertFalse(self.sink.send(b"x"))

    def test_new_connection_replaces_idle_old_one(self):
        # B1: the old client is silent and nothing has been sent, so no send
        # failure can reveal that it is dead. The new one must still get in.
        connects = []
        self.sink = d.FrameSink(
            0, on_connect=lambda attach: (attach(), connects.append(1)))
        old = self._connect()
        self.assertTrue(wait_until(lambda: len(connects) == 1))
        new = self._connect()
        self.assertTrue(wait_until(lambda: len(connects) == 2))
        self.assertTrue(self.sink.send(b"fresh"))
        self.assertEqual(recv_exact(new, 5), b"fresh")
        self.assertEqual(recv_exact(old, 1), b"")        # closed by the sink

    def test_stalled_client_is_dropped_within_send_timeout(self):
        # B2: a client that stops reading must not block the sender forever.
        self.sink = d.FrameSink(0, send_timeout=0.2)
        self._connect()                                  # never reads
        self.assertTrue(wait_until(self.sink.has_client))
        chunk = b"\0" * (1 << 20)
        t0 = time.monotonic()
        while self.sink.send(chunk):
            self.assertLess(time.monotonic() - t0, 5, "send never gave up")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertFalse(self.sink.has_client())

    def test_client_is_not_registered_until_handler_attaches(self):
        release = threading.Event()

        def on_connect(attach):
            release.wait(2)
            attach()

        self.sink = d.FrameSink(0, on_connect=on_connect)
        self._connect()
        time.sleep(0.1)
        self.assertFalse(self.sink.has_client())
        release.set()
        self.assertTrue(wait_until(self.sink.has_client))


# ---------------------------------------------------------------------------

FRAME = 8        # bytes per fake raw frame

# Stand-in for ffmpeg: every FRAME bytes on stdin become one "packet" on
# stdout, with its size reported on the side-channel fd in framecrc format.
FAKE_ENCODER = r"""
import os, sys
fd = int(sys.argv[1]); n = 0
while True:
    buf = b""
    while len(buf) < %d:
        chunk = os.read(0, %d - len(buf))
        if not chunk:
            sys.exit(0)
        buf += chunk
    os.write(fd, b"0, %%d, %%d, 1, %%d, 0x0\n" %% (n, n, len(buf)))
    os.write(1, buf)
    n += 1
""" % (FRAME, FRAME)


def fake_cmd(args, mode, sizes_fd):
    return [sys.executable, "-c", FAKE_ENCODER, str(sizes_fd)]


class EncoderTest(unittest.TestCase):
    def setUp(self):
        self.shutdown = threading.Event()
        self.sink = d.FrameSink(0)
        self.client = socket.create_connection(("127.0.0.1", self.sink.port),
                                               timeout=2)
        self.assertTrue(wait_until(self.sink.has_client))
        self.enc = None

    def tearDown(self):
        self.shutdown.set()
        if self.enc is not None:
            self.enc.stop()
        self.client.close()
        self.sink.shutdown()

    def _encoder(self, build_cmd=fake_cmd):
        self.enc = d.Encoder(default_args(), self.sink, self.shutdown,
                             build_cmd=build_cmd)
        return self.enc

    def test_written_frame_reaches_client_length_prefixed(self):
        enc = self._encoder()
        enc.start(d.DESKTOP_MODE)
        enc.write(b"ABCDEFGH")
        self.assertEqual(recv_exact(self.client, 12),
                         struct.pack(">I", 8) + b"ABCDEFGH")

    def test_restart_switches_mode_and_keeps_streaming(self):
        enc = self._encoder()
        enc.start(d.DESKTOP_MODE)
        enc.write(b"11111111")
        self.assertEqual(recv_exact(self.client, 12)[4:], b"11111111")
        enc.restart(d.VIDEO_MODE, why="test", grace=1.0)
        self.assertIs(enc.mode, d.VIDEO_MODE)
        enc.write(b"22222222")
        self.assertEqual(recv_exact(self.client, 12)[4:], b"22222222")

    def test_restart_leaves_no_orphan_process(self):
        enc = self._encoder()
        enc.start(d.DESKTOP_MODE)
        old = enc.proc
        enc.restart(d.DESKTOP_MODE, why="test", grace=1.0)
        self.assertIsNotNone(old.poll())

    def test_write_respawns_encoder_that_died(self):
        # B5: one encoder crash must not take the daemon (and display) down.
        enc = self._encoder()
        enc.start(d.DESKTOP_MODE)
        enc.proc.kill()
        enc.proc.wait()
        enc.write(b"AFTERDIE")
        self.assertEqual(recv_exact(self.client, 12)[4:], b"AFTERDIE")

    def test_gives_up_when_encoder_keeps_dying(self):
        enc = self._encoder(build_cmd=lambda a, m, fd: ["false"])
        enc.start(d.DESKTOP_MODE)
        with self.assertRaises(d.EncoderDead):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                enc.write(b"x" * FRAME)
                time.sleep(0.01)


# ---------------------------------------------------------------------------

class BuildFfmpegCmdTest(unittest.TestCase):
    def test_reports_packet_sizes_on_the_given_fd(self):
        cmd = d.build_ffmpeg_cmd(default_args(), d.DESKTOP_MODE, 42)
        self.assertIn("pipe:42", cmd[-1])
        self.assertIn("pipe:1", cmd[-1])

    def test_vbr_rates_come_from_the_mode(self):
        cmd = d.build_ffmpeg_cmd(default_args(), d.VIDEO_MODE, 3)
        self.assertEqual(cmd[cmd.index("-b:v") + 1], d.VIDEO_MODE["bitrate"])
        self.assertEqual(cmd[cmd.index("-maxrate") + 1], d.VIDEO_MODE["maxrate"])


def nal_types(au: bytes):
    types_, i = [], 0
    while True:
        i = au.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(au):
            return types_
        types_.append(au[i + 3] & 0x1F)
        i += 3


HAVE_VAAPI = bool(shutil.which("ffmpeg")) and os.access(d.VAAPI_DEV, os.R_OK | os.W_OK)


@unittest.skipUnless(HAVE_VAAPI, "needs ffmpeg and a usable VAAPI render node")
class RealEncoderTest(unittest.TestCase):
    """Drives the real ffmpeg command line with synthetic frames."""

    def _encode(self, n_frames, gap):
        r, w = os.pipe()
        proc = subprocess.Popen(
            d.build_ffmpeg_cmd(default_args(), d.DESKTOP_MODE, w),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, pass_fds=(w,), bufsize=0)
        os.close(w)
        aus = []
        reader = threading.Thread(
            target=lambda: aus.extend(
                d.read_access_units(proc.stdout.fileno(), r)),
            daemon=True)
        reader.start()
        frame = bytearray(d.FRAME_W * d.FRAME_H * 4)
        for i in range(n_frames):
            frame[i * 4096:(i + 1) * 4096] = bytes([255]) * 4096
            proc.stdin.write(frame)
            time.sleep(gap)
        proc.stdin.close()
        proc.wait(timeout=20)
        reader.join(timeout=5)
        os.close(r)
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.stdout.close()
        proc.stderr.close()
        return aus, err, proc.returncode

    def test_one_aud_led_access_unit_per_input_frame(self):
        aus, err, rc = self._encode(n_frames=6, gap=0.0)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(aus), 6)
        for au in aus:
            self.assertEqual(nal_types(au)[0], 9, "AU must start with an AUD")
        self.assertIn(5, nal_types(aus[0]), "stream must open with an IDR")
        self.assertIn(7, nal_types(aus[0]), "stream must open with an SPS")

    def test_single_frame_is_emitted_without_further_input(self):
        # A1 on the sender side: the last frame of a burst (and the cached
        # frame injected for a new client) must come out on its own, with
        # stdin still open and nothing following it.
        r, w = os.pipe()
        proc = subprocess.Popen(
            d.build_ffmpeg_cmd(default_args(), d.DESKTOP_MODE, w),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, pass_fds=(w,), bufsize=0)
        os.close(w)
        aus = []
        gen = d.read_access_units(proc.stdout.fileno(), r)
        reader = threading.Thread(target=lambda: aus.append(next(gen)),
                                  daemon=True)
        reader.start()
        try:
            proc.stdin.write(bytes(d.FRAME_W * d.FRAME_H * 4))
            reader.join(timeout=3)
            self.assertEqual(len(aus), 1, "encoder held the only frame back")
            self.assertIn(5, nal_types(aus[0]))
        finally:
            proc.kill()
            proc.wait()
            reader.join(timeout=2)
            os.close(r)
            proc.stdin.close()
            proc.stdout.close()

    def test_keyframes_follow_wall_clock_at_low_fps(self):
        # A2: 10 frames over ~2.5 s must yield an IDR about every 0.5 s,
        # not one IDR per 30 frames.
        aus, err, rc = self._encode(n_frames=10, gap=0.25)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(aus), 10, "frames must not be duplicated/dropped")
        idrs = sum(1 for au in aus if 5 in nal_types(au))
        self.assertGreaterEqual(idrs, 4, err)

    def test_burst_produces_no_timestamp_warnings(self):
        aus, err, rc = self._encode(n_frames=40, gap=0.0)
        self.assertEqual(len(aus), 40)
        self.assertEqual(err.strip(), "")


def read_framed_au(sock, timeout=5.0):
    header = recv_exact(sock, 4, timeout)
    if len(header) < 4:
        return None
    (n,) = struct.unpack(">I", header)
    return recv_exact(sock, n, timeout)


class FakeRect:
    x1, y1, x2, y2 = 0, 0, 64, 64


class FakeBuffer:
    width, height = d.FRAME_W, d.FRAME_H
    rects = [FakeRect()]

    def __init__(self):
        self.bytes = memoryview(bytearray(d.FRAME_W * d.FRAME_H * 4))


class FakeCard:
    """Stands in for PyEvdi.Card: runs a scripted list of steps, one per
    handle_events() call, then stops the daemon like Ctrl-C would."""
    script = []

    def __init__(self, idx):
        self.acquire_framebuffer_handler = None
        self.mode_changed_handler = None
        self._steps = iter(self.script)

    def connect(self, *a):
        pass

    def disconnect(self):
        pass

    def close(self):
        pass

    def handle_events(self, timeout_ms):
        step = next(self._steps, None)
        if step is None:
            raise KeyboardInterrupt
        step(self)


@unittest.skipUnless(HAVE_VAAPI, "needs ffmpeg and a usable VAAPI render node")
class RunLoopTest(unittest.TestCase):
    """run() end to end with a scripted evdi card and the real encoder."""

    def setUp(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.old_sigterm = signal.getsignal(signal.SIGTERM)
        self.socks = []

    def tearDown(self):
        signal.signal(signal.SIGTERM, self.old_sigterm)
        for s in self.socks:
            s.close()

    def _connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        self.socks.append(s)
        return s

    def _run(self, script):
        fake = types.SimpleNamespace(
            AVAILABLE=1, check_device=lambda i: 1,
            Card=type("Card", (FakeCard,), {"script": script}))
        with mock.patch.object(d, "PyEvdi", fake):
            return d.run(default_args("--port", str(self.port)))

    def test_live_frame_reaches_client_without_a_following_frame(self):
        got = {}

        def connect(card):
            got["sock"] = self._connect()
            time.sleep(0.5)                 # let the connect handler finish

        def one_frame(card):
            card.acquire_framebuffer_handler(FakeBuffer())

        def read(card):
            got["au"] = read_framed_au(got["sock"])

        self.assertEqual(self._run([connect, one_frame, read]), 0)
        self.assertEqual(nal_types(got["au"])[0], 9)
        self.assertIn(5, nal_types(got["au"]))

    def test_reconnecting_client_gets_idr_from_cached_frame(self):
        # Static screen: one frame was shown, then nothing changes. A client
        # that connects afterwards must still get a picture immediately.
        got = {}

        def one_frame(card):
            card.acquire_framebuffer_handler(FakeBuffer())

        def connect_and_read(card):
            got["au"] = read_framed_au(self._connect())

        self.assertEqual(self._run([one_frame, connect_and_read]), 0)
        self.assertIsNotNone(got["au"])
        self.assertIn(7, nal_types(got["au"]))
        self.assertIn(5, nal_types(got["au"]))

    def test_second_client_takes_over_on_static_screen(self):
        got = {}

        def one_frame(card):
            card.acquire_framebuffer_handler(FakeBuffer())

        def first_client(card):
            got["first"] = self._connect()
            got["first_au"] = read_framed_au(got["first"])

        def second_client(card):
            got["second_au"] = read_framed_au(self._connect())

        self.assertEqual(
            self._run([one_frame, first_client, second_client]), 0)
        self.assertIn(5, nal_types(got["first_au"]))
        self.assertIn(5, nal_types(got["second_au"]))


if __name__ == "__main__":
    unittest.main()
