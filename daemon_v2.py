#!/usr/bin/env python3
"""
tabdisplay daemon: evdi -> VAAPI H.264 -> TCP -> tablet MediaCodec.

Layout:
  - Main thread pumps PyEvdi events. The acquire_framebuffer callback runs
    here and writes evdi's raw BGRX frame to ffmpeg's stdin.
  - ffmpeg subprocess: rawvideo in -> h264_vaapi out. Annex B on stdout,
    plus one line per encoded packet (its size) on a side-channel pipe.
  - "ffmpeg-stdout" thread: pairs the two into complete access units and
    sends each to the client as [u32 big-endian length][AU bytes].
  - "acceptor" thread: TCP listener on 7070; newest client wins.

Wire protocol: a sequence of length-prefixed access units, nothing else.
Each AU is Annex B, starts with an AUD, and the first AU after a connect is
always SPS+PPS+IDR. The length prefix is what lets the receiver decode a
frame the moment it arrives instead of waiting for the next one to
delimit it.

Backpressure: writes to ffmpeg.stdin block when the encoder or the client
can't keep up, which naturally throttles PyEvdi event processing. Encoded
output is dropped when no client is attached. On (re)connect the daemon
restarts ffmpeg and injects the most recent frame into the fresh encoder,
so the client's first bytes are a SPS+PPS+IDR access unit.
"""
import argparse
import collections
import logging
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import PyEvdi


FRAME_W = 2560
FRAME_H = 1600
PORT = 7070
VAAPI_DEV = "/dev/dri/renderD128"

# Adaptive-rate profiles. The MotionDetector picks between these based on
# per-frame dirty-rect coverage and the daemon restarts ffmpeg with the new
# parameters on each transition. Identity (``is``) comparison is used to
# distinguish them, so don't replace these dicts at runtime — mutate the
# desktop dict in-place when a CLI override lands.
DESKTOP_MODE = {
    "fps":     60,
    "bitrate": "20M",
    "maxrate": "35M",
    "bufsize": "35M",
    "label":   "desktop",
}
VIDEO_MODE = {
    "fps":     30,
    "bitrate": "8M",
    "maxrate": "15M",
    "bufsize": "15M",
    "label":   "video",
}

DEFAULT_EDID = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "edid_2560x1600.bin")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
)
log = logging.getLogger("tabdisplay")


# ---------------------------------------------------------------------------
# Motion detector for adaptive desktop/video mode switching
# ---------------------------------------------------------------------------

class MotionDetector:
    """Decide when to switch between DESKTOP_MODE (60 fps, high bitrate) and
    VIDEO_MODE (30 fps, low bitrate) based on the per-frame dirty-rect
    coverage ratio reported by evdi.

    A high coverage ratio (mutter rewriting most of the framebuffer every
    frame) is a strong signature of fullscreen video playback. Dragged
    windows, terminal scrolling, etc. only dirty a fraction of the screen
    each frame so they stay safely under the threshold.

    State machine:
      ratio >= THRESHOLD_HIGH       -> high_streak++, low_streak = 0
      ratio <  THRESHOLD_LOW        -> low_streak++,  high_streak = 0
      otherwise (middle band)       -> both streaks reset
      high_streak >= N_HIGH         -> emit 'video' transition
      low_streak  >= N_LOW          -> emit 'desktop' transition

    The detector always emits the transition it has decided; the caller is
    responsible for ignoring transitions that match the current mode (which
    happens repeatedly while the dirty pattern persists).
    """

    THRESHOLD_HIGH = 0.80
    THRESHOLD_LOW  = 0.40
    N_HIGH         = 8                  # ~0.13 s at 60 fps to enter video
    N_LOW          = 30                 # ~1.0  s at 30 fps to leave video

    def __init__(self, threshold_high: float = None,
                 threshold_low: float = None):
        self.th_high = self.THRESHOLD_HIGH if threshold_high is None else threshold_high
        self.th_low  = self.THRESHOLD_LOW  if threshold_low  is None else threshold_low
        self._high_streak = 0
        self._low_streak  = 0
        # Exposed for log messages on transition.
        self.last_streak  = 0
        self.last_ratio   = 0.0

    def update(self, ratio: float):
        self.last_ratio = ratio
        if ratio >= self.th_high:
            self._high_streak += 1
            self._low_streak   = 0
            if self._high_streak >= self.N_HIGH:
                self.last_streak  = self._high_streak
                self._high_streak = 0
                return "video"
        elif ratio < self.th_low:
            self._low_streak  += 1
            self._high_streak  = 0
            if self._low_streak >= self.N_LOW:
                self.last_streak = self._low_streak
                self._low_streak = 0
                return "desktop"
        else:
            # Middle band breaks both consecutive streaks. The state-machine
            # only fires on uninterrupted runs above/below the thresholds.
            self._high_streak = 0
            self._low_streak  = 0
        return None


class WarmupGate:
    """Suppress detector decisions during daemon startup.

    mutter's initial render burst when evdi first connects pushes several
    ratio=1.00 frames, which would instantly trip desktop -> video and then
    stick through the 30-frame low-coverage hysteresis (~30 s wall clock at
    idle fps). The gate withholds detector updates until either one frame
    with coverage below THRESHOLD_LOW arrives (screen has settled) or
    MAX_FRAMES have gone by (safety cap for "daemon started mid-video").
    """

    MAX_FRAMES = 150                  # ~2.5 s at 60 fps

    def __init__(self):
        self.frames = 0
        self.settled = False

    def allow(self, ratio: float, th_low: float) -> bool:
        """Return True once detection may proceed; consumes ratios until then."""
        if self.settled:
            return True
        self.frames += 1
        if ratio < th_low or self.frames >= self.MAX_FRAMES:
            self.settled = True
            log.info("detector warm-up complete after %d frames%s",
                     self.frames,
                     "" if ratio < th_low else " (cap hit; screen busy)")
            return True
        return False


# ---------------------------------------------------------------------------
# TCP sink (single client, newest connection wins)
# ---------------------------------------------------------------------------

class FrameSink:
    """Single-client TCP sink.

    A new connection always replaces the current client. The tablet only
    ever opens one socket, so a second connection means the first is dead
    even if no send has failed yet (on a static screen nothing is sent, so
    nothing *can* fail).

    ``on_connect``, when supplied, is invoked from the acceptor thread after
    accept() but BEFORE the new socket is registered for sends. It receives
    an ``attach`` callable; calling it registers the client. This ordering
    lets the handler swap ffmpeg first, so a fresh client never receives
    tail bytes of the previous encoder's stream:

        restart_ffmpeg()   # old relays drained + joined
        attach()           # client now eligible to receive
        inject cached frame# forces immediate SPS+PPS+IDR out of new ffmpeg

    ``send_timeout`` bounds how long a client that has stopped reading can
    stall the pipeline before it is dropped.
    """

    def __init__(self, port: int, on_connect=None, host: str = "127.0.0.1",
                 send_timeout: float = 2.0):
        self._on_connect = on_connect
        self._send_timeout = send_timeout
        self._lock = threading.Lock()
        self._conn = None
        self._shutdown = threading.Event()
        # Bind here, not on the acceptor thread, so "address already in use"
        # fails the daemon instead of silently killing a background thread.
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind((host, port))
            self._srv.listen(1)
        except OSError:
            self._srv.close()
            raise
        self._srv.settimeout(1.0)
        self.host, self.port = self._srv.getsockname()
        log.info("listening on %s:%d", self.host, self.port)
        self._thread = threading.Thread(target=self._serve, name="acceptor",
                                        daemon=True)
        self._thread.start()

    def _register(self, conn) -> None:
        with self._lock:
            self._conn = conn

    def _drop(self, conn=None) -> None:
        """Close ``conn`` if it is still the current client (any current
        client when ``conn`` is None)."""
        with self._lock:
            if self._conn is None or (conn is not None and self._conn is not conn):
                return
            old, self._conn = self._conn, None
        try:
            # shutdown() wakes a sender blocked on this socket; close() alone
            # does not.
            old.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        old.close()

    def _serve(self) -> None:
        while not self._shutdown.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break                 # listener closed by shutdown()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(self._send_timeout)
            if self.has_client():
                log.info("new connection; dropping previous client")
                self._drop()
            log.info("client connected from %s", addr)

            def attach(c=conn):
                self._register(c)

            if self._on_connect is not None:
                try:
                    self._on_connect(attach)
                except Exception:
                    log.exception("client-connect handler failed")
                    attach()          # never leave a client unregistered
            else:
                attach()

    def send(self, data: bytes) -> bool:
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.sendall(data)
            return True
        except OSError as exc:        # includes timeouts and resets
            log.warning("send failed (%s) — dropping client", exc)
            self._drop(conn)
            return False

    def has_client(self) -> bool:
        with self._lock:
            return self._conn is not None

    def shutdown(self) -> None:
        self._shutdown.set()
        self._srv.close()
        self._drop()


# ---------------------------------------------------------------------------
# ffmpeg subprocess plumbing
# ---------------------------------------------------------------------------

def build_ffmpeg_cmd(args, mode: dict, sizes_fd: int) -> list:
    """rawvideo BGR0 in -> h264 Annex B on stdout, VAAPI encode.

    ``mode`` is one of DESKTOP_MODE / VIDEO_MODE and supplies fps, bitrate,
    maxrate, bufsize. The rest of the encoder configuration (profile, GOP,
    AUD, colour metadata, CSC location) is shared between modes.

    ``sizes_fd`` is an inherited pipe on which ffmpeg reports the size of
    every encoded packet (see read_access_units).
    """
    # Stamp frames with their wall-clock arrival time. A rawvideo pipe has
    # no timestamps, so ffmpeg would otherwise count 1/fps per frame and
    # every time-based option below would silently become frame-count
    # based (at 5 fps idle, "0.5 s" between IDRs would really be 6 s).
    # Microsecond time base so two frames can never share a timestamp.
    stamp = "settb=1/1000000,setpts=RTCTIME-RTCSTART"

    # CSC (BGR->NV12) location:
    #   --csc gpu (default): VAAPI's scale_vaapi does CSC on-chip, freeing
    #     the CPU. Requires explicit BT.709 limited-range tagging on the
    #     output so the decoder reads back what the VPP block actually wrote.
    #   --csc cpu: libswscale on the CPU, ~10-15% of one core at
    #     2560x1600@60. No explicit colour-metadata tags — the decoder's
    #     resolution-based BT.709 inference matches well enough.
    if args.csc == "gpu":
        vf = (f"{stamp},hwupload,scale_vaapi=format=nv12:"
              "out_color_matrix=bt709:out_range=limited")
    else:
        vf = f"{stamp},format=nv12,hwupload"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-init_hw_device", f"vaapi=va:{args.vaapi_device}",
        "-filter_hw_device", "va",
        "-f", "rawvideo",
        # evdi's buffer is XRGB8888 little-endian = B,G,R,X in memory,
        # which is exactly ffmpeg's "bgr0".
        "-pix_fmt", "bgr0",
        "-s", f"{FRAME_W}x{FRAME_H}",
        "-r", str(mode["fps"]),
        "-i", "pipe:0",
        "-vf", vf,
        # One packet out per frame in: never duplicate or drop frames to
        # hit a constant rate, and keep the filter's microsecond stamps.
        "-fps_mode", "passthrough",
        "-enc_time_base", "1:1000000",
        "-c:v", "h264_vaapi",
        "-profile:v", "high",
        "-level", "5.1",
        # Wall-clock keyframes (true only because of the setpts stamp
        # above), NOT a frame-count GOP: at mutter's idle fps a -g 30 GOP
        # stretches to several seconds without an IDR, so a dropped or
        # corrupted AU on the receiver would freeze the picture for that
        # long. 0.5 s bounds recovery identically at every fps.
        "-force_key_frames",
        "expr:gte(t,n_forced*%.3f)" % args.keyframe_interval_sec,
        "-bf", "0",
        "-quality", str(args.quality),
        # Encoder pipeline depth: 1 = output AU surfaced synchronously per
        # input frame (~1 frame less glass-to-glass latency). Costs ~35%
        # throughput headroom (89 vs 139 fps synthetic at this resolution),
        # still well clear of the 60 fps budget.
        "-async_depth", str(args.async_depth),
        # Access Unit Delimiter NAL (type 9) at the start of every frame.
        "-aud", "true",
    ]

    # When CSC happens on the GPU we *must* tag the SPS so the decoder reads
    # the YUV correctly — VAAPI's VPP writes BT.709 limited but, absent VUI
    # tags, the decoder might assume otherwise.
    if args.csc == "gpu":
        cmd += [
            "-color_primaries", "bt709",
            "-color_trc",       "bt709",
            "-colorspace",      "bt709",
            "-color_range",     "tv",
        ]

    if args.rc_mode == "CQP":
        # Constant QP — encoder spends bits as needed per frame to hold quality.
        # On this AMD VCN driver, CQP/CBR/VBR are the supported modes (ICQ
        # is listed by ffmpeg generically but rejected at runtime).
        cmd += ["-rc_mode", "CQP", "-qp", str(args.qp)]
    elif args.rc_mode == "VBR":
        # Average-target with headroom cap. Bitrate parameters come from the
        # active mode (desktop or video) so they swap on transition.
        cmd += ["-rc_mode", "VBR",
                "-b:v", mode["bitrate"], "-maxrate", mode["maxrate"],
                "-bufsize", mode["bufsize"]]
    elif args.rc_mode == "CBR":
        # Strict CBR (legacy v0 mode). Tight bit budgets cause heavy blocking
        # on motion at low bitrates — kept for A/B comparison only.
        cmd += ["-rc_mode", "CBR",
                "-b:v", mode["bitrate"], "-maxrate", mode["bitrate"],
                "-bufsize", mode["bufsize"]]
    else:
        raise ValueError(f"unknown rc_mode {args.rc_mode}")

    # Two outputs of the same packets: the "framecrc" muxer prints one text
    # line per packet including its byte size, and the raw h264 muxer writes
    # the packet itself. framecrc is listed first so the size is already
    # readable when the bytes arrive.
    cmd += ["-map", "0:v", "-f", "tee",
            f"[f=framecrc]pipe:{sizes_fd}|[f=h264]pipe:1"]
    return cmd


def read_access_units(data_fd: int, sizes_fd: int):
    """Yield each encoded access unit as soon as its last byte is read.

    Annex B only marks where an AU starts, so a reader of the bare stream
    cannot know an AU is complete until the next one begins — one whole
    frame interval later. ffmpeg knows the packet size, and reports it on
    ``sizes_fd`` as framecrc lines:

        #comment
        stream, dts, pts, duration, size, crc[, ...]

    Reads both pipes in whatever order they become ready, so it cannot
    deadlock against ffmpeg regardless of which one ffmpeg writes first.
    An incomplete trailing AU at EOF is discarded.
    """
    sel = selectors.DefaultSelector()
    sel.register(data_fd, selectors.EVENT_READ)
    sel.register(sizes_fd, selectors.EVENT_READ)
    open_fds = {data_fd, sizes_fd}
    data = bytearray()
    text = b""
    sizes = collections.deque()
    try:
        # After data EOF, keep going only while a size line could still
        # complete what is already buffered.
        while data_fd in open_fds or (sizes_fd in open_fds and data):
            for key, _ in sel.select():
                chunk = os.read(key.fd, 256 * 1024)
                if not chunk:
                    sel.unregister(key.fd)
                    open_fds.discard(key.fd)
                elif key.fd == data_fd:
                    data += chunk
                else:
                    *lines, text = (text + chunk).split(b"\n")
                    for line in lines:
                        if line and not line.startswith(b"#"):
                            sizes.append(int(line.split(b",")[4]))
            while sizes and len(data) >= sizes[0]:
                n = sizes.popleft()
                au = bytes(data[:n])
                del data[:n]
                yield au
    finally:
        sel.close()


def frame_au(au: bytes) -> bytes:
    """Wire framing: 4-byte big-endian length, then the access unit."""
    return struct.pack(">I", len(au)) + au


def relay_stdout(proc: subprocess.Popen, sizes_fd: int,
                 sink: FrameSink) -> None:
    """Forward ffmpeg's output to the current client, one whole AU per
    send. Owns ``sizes_fd``. Exits at EOF, i.e. when ffmpeg does."""
    try:
        for au in read_access_units(proc.stdout.fileno(), sizes_fd):
            sink.send(frame_au(au))
    except (OSError, ValueError) as exc:
        log.warning("relay_stdout stopped: %s", exc)
    finally:
        os.close(sizes_fd)
    log.info("relay_stdout exiting")


def relay_stderr(proc: subprocess.Popen, shutdown: threading.Event) -> None:
    """Surface ffmpeg warnings/errors into our log stream."""
    try:
        for raw in iter(proc.stderr.readline, b""):
            if shutdown.is_set():
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                log.info("[ffmpeg] %s", line)
    except (ValueError, OSError):
        pass    # pipes closed by a concurrent restart — expected


class EncoderDead(RuntimeError):
    """ffmpeg keeps exiting; respawning it is not helping."""


class Encoder:
    """The ffmpeg subprocess and its relay threads.

    ``lock`` serialises everything that touches the process: frame writes
    and mode-switch restarts on the evdi callback thread, and
    client-connect restarts on the acceptor thread. It is re-entrant so a
    caller can hold it across a restart + write sequence.
    """

    # More than this many unexpected exits inside the window means the
    # encoder cannot run at all (bad device, bad flags) — stop retrying.
    MAX_RESPAWNS = 3
    RESPAWN_WINDOW = 10.0

    def __init__(self, args, sink: FrameSink, shutdown: threading.Event,
                 build_cmd=build_ffmpeg_cmd):
        self._args = args
        self._sink = sink
        self._shutdown = shutdown
        self._build_cmd = build_cmd
        self._threads = []
        self._respawns = collections.deque(maxlen=self.MAX_RESPAWNS)
        self.lock = threading.RLock()
        self.proc = None
        self.mode = None

    def start(self, mode: dict) -> None:
        with self.lock:
            self._spawn(mode)

    def _spawn(self, mode: dict) -> None:
        sizes_r, sizes_w = os.pipe()
        try:
            cmd = self._build_cmd(self._args, mode, sizes_w)
            log.info("ffmpeg cmd (%s, %d fps, %s/%s): %s",
                     mode["label"], mode["fps"], mode["bitrate"],
                     mode["maxrate"], " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                pass_fds=(sizes_w,),
            )
        except BaseException:
            os.close(sizes_r)
            raise
        finally:
            # Our copy of the write end must go, or the reader never sees
            # EOF when ffmpeg exits.
            os.close(sizes_w)
        stdout_thr = threading.Thread(
            target=relay_stdout, args=(proc, sizes_r, self._sink),
            name=f"ffmpeg-stdout/{mode['label']}", daemon=True)
        stderr_thr = threading.Thread(
            target=relay_stderr, args=(proc, self._shutdown),
            name=f"ffmpeg-stderr/{mode['label']}", daemon=True)
        stdout_thr.start()
        stderr_thr.start()
        self.proc = proc
        self.mode = mode
        self._threads = [stdout_thr, stderr_thr]

    def _reap(self, grace: float) -> None:
        """Stop the current ffmpeg and wait for its relay threads."""
        proc, threads = self.proc, self._threads
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass
        # Unblock ffmpeg's demuxer read: glibc installs SIGTERM with
        # SA_RESTART, so a blocking read() is transparently resumed and the
        # termination flag is only noticed once the read returns. At idle
        # fps that can stall the exit for a second or more. Closing stdin
        # forces an immediate EOF.
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            log.warning("old ffmpeg did not exit in %.1f s; killing", grace)
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

        # The process is gone, so the relay threads see EOF and finish on
        # their own. Wait for them so two encoders can never interleave
        # bytes into the same client socket. Only close the pipes once the
        # threads are done: closing an fd under a live reader would let the
        # next ffmpeg's pipes reuse the number.
        for t in threads:
            t.join(timeout=3)
        if any(t.is_alive() for t in threads):
            log.warning("relay thread still running after ffmpeg exit")
            return
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def restart(self, mode: dict, why: str = "auto-mode",
                grace: float = 2.0) -> None:
        """Swap ffmpeg for a fresh one running ``mode``.

        SIGTERM rather than a stdin-EOF flush: in-flight bytes don't matter
        because the new stream opens with SPS+PPS+IDR, which resyncs the
        tablet decoder. ``grace`` bounds the wait for a clean exit before
        SIGKILL: mode switches keep the 2 s default, client-connect
        restarts pass a short grace since nobody is consuming the old
        stream's tail and the AMD VAAPI process occasionally ignores
        SIGTERM for seconds. Runs on the evdi callback thread for mode
        switches, so a 50-200 ms hiccup is tolerable.
        """
        with self.lock:
            log.info("ffmpeg restart (%s): %s -> %s",
                     why, self.mode["label"], mode["label"])
            self._reap(grace)
            self._spawn(mode)

    def write(self, payload) -> None:
        """Feed one raw frame. If ffmpeg has died, respawn it and feed the
        frame to the new one; raise EncoderDead when that keeps failing."""
        with self.lock:
            while True:
                try:
                    view = memoryview(payload)
                    while len(view):
                        # Raw (unbuffered) pipe writes may be short.
                        view = view[self.proc.stdin.write(view):]
                    return
                except (OSError, ValueError) as exc:
                    self._respawn(exc)

    def _respawn(self, exc: Exception) -> None:
        now = time.monotonic()
        recent = [t for t in self._respawns if now - t < self.RESPAWN_WINDOW]
        if len(recent) >= self.MAX_RESPAWNS:
            raise EncoderDead(
                f"ffmpeg exited {len(recent) + 1} times in "
                f"{self.RESPAWN_WINDOW:.0f} s") from exc
        self._respawns.append(now)
        log.error("ffmpeg stdin write failed (%s; exit code %s); respawning",
                  exc, self.proc.poll())
        self._reap(grace=0.3)
        self._spawn(self.mode)

    def stop(self) -> None:
        with self.lock:
            if self.proc is not None:
                self._reap(grace=2.0)


# ---------------------------------------------------------------------------
# evdi setup
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


def run(args) -> int:
    idx = pick_evdi_card()
    if idx < 0:
        log.error("no AVAILABLE evdi card; is the kernel module loaded?")
        return 2

    with open(args.edid, "rb") as fh:
        edid = fh.read()
    log.info("loaded EDID %s (%d bytes)", args.edid, len(edid))

    # Apply CLI overrides to DESKTOP_MODE in place so identity (`is`)
    # comparisons against DESKTOP_MODE keep working in the callback.
    DESKTOP_MODE["bitrate"] = args.bitrate
    DESKTOP_MODE["maxrate"] = args.maxrate
    DESKTOP_MODE["bufsize"] = args.bufsize
    log.info("desktop mode: %d fps, %s/%s/%s",
             DESKTOP_MODE["fps"], DESKTOP_MODE["bitrate"],
             DESKTOP_MODE["maxrate"], DESKTOP_MODE["bufsize"])
    log.info("video mode:   %d fps, %s/%s/%s%s",
             VIDEO_MODE["fps"], VIDEO_MODE["bitrate"],
             VIDEO_MODE["maxrate"], VIDEO_MODE["bufsize"],
             " (auto-mode DISABLED)" if args.no_auto_mode else "")

    shutdown = threading.Event()

    # ``last_payload`` holds a private snapshot of the most recent raw frame
    # (refreshed at most twice per second) so a newly connected tablet can be
    # handed an immediate keyframe even when the screen is static.
    state = {
        "encoder":       None,
        "frame_counter": 0,
        "last_payload":  None,
    }

    def handle_client_connect(attach):
        """Runs on the acceptor thread before the client is registered.

        A fresh ffmpeg emits SPS+PPS+IDR for its very first frame, so
        restarting here both discards any stale tail of the previous stream
        and guarantees a decoder-sync point. Feeding the cached frame makes
        that AU appear immediately even when mutter is idle.
        """
        encoder = state["encoder"]
        if encoder is None:            # still starting up; nothing to swap
            attach()
            return
        try:
            with encoder.lock:
                encoder.restart(encoder.mode, why="client connect", grace=0.3)
                attach()
                payload = state["last_payload"]
                if payload is not None:
                    encoder.write(payload)
                    log.info("injected cached frame; new client has an "
                             "immediate IDR")
                else:
                    log.info("no cached frame yet; client syncs on next "
                             "evdi update")
        except Exception:
            log.exception("client-connect resync failed; registering client "
                          "without injection")
            attach()

    sink = FrameSink(args.port, on_connect=handle_client_connect,
                     host=args.bind)

    encoder = Encoder(args, sink, shutdown)
    encoder.start(DESKTOP_MODE)
    state["encoder"] = encoder

    detector = MotionDetector(
        threshold_high=args.video_threshold,
        threshold_low=args.desktop_threshold,
    )
    warmup = WarmupGate()

    card = PyEvdi.Card(idx)
    log.info("opened evdi card %d", idx)

    stats = {"frames": 0, "dropped": 0, "last_log": time.monotonic(),
             "ms_write_total": 0.0, "last_cache": 0.0}

    def on_mode_changed(mode):
        log.info("mode_changed: %dx%d @ %d Hz, bpp=%d, pixel_format=0x%x",
                 mode.width, mode.height, mode.refresh_rate,
                 mode.bits_per_pixel, mode.pixel_format)

    inv_screen = 1.0 / float(FRAME_W * FRAME_H)

    def on_acquire_framebuffer(buf):
        # 1) Auto-mode: feed dirty coverage into the detector and react.
        if not args.no_auto_mode:
            dirty_area = 0
            for r in buf.rects:
                dirty_area += (r.x2 - r.x1) * (r.y2 - r.y1)
            ratio = dirty_area * inv_screen
            if ratio > 1.0:
                ratio = 1.0
            if not warmup.allow(ratio, detector.th_low):
                pass                    # settling: skip detection, keep sending
            else:
                transition = detector.update(ratio)
                if transition == "video" and encoder.mode is DESKTOP_MODE:
                    log.info("mode transition: desktop -> video "
                             "(ratio=%.2f, streak=%d)",
                             ratio, detector.last_streak)
                    encoder.restart(VIDEO_MODE)
                elif transition == "desktop" and encoder.mode is VIDEO_MODE:
                    log.info("mode transition: video -> desktop "
                             "(ratio=%.2f, streak=%d)",
                             ratio, detector.last_streak)
                    encoder.restart(DESKTOP_MODE)

        # 2) Frame dropping in video mode: keep every other frame so the
        # encoder gets ~30 fps. (pyevdi exposes no explicit "release buffer"
        # method on the Buffer object; returning is equivalent.)
        state["frame_counter"] += 1
        if encoder.mode is VIDEO_MODE and state["frame_counter"] % 2 != 0:
            stats["dropped"] += 1
            return

        # 3) Send the frame to ffmpeg, verbatim.
        payload = buf.bytes
        t0 = time.monotonic()
        try:
            encoder.write(payload)
        except EncoderDead as exc:
            log.error("giving up: %s", exc)
            shutdown.set()
            return
        t1 = time.monotonic()

        # Refresh the connect-time IDR snapshot. buf.bytes aliases evdi's
        # internal buffer, so it must be copied out; throttle the 16 MB
        # copy to twice per second.
        if t1 - stats["last_cache"] >= 0.5:
            state["last_payload"] = bytes(payload)
            stats["last_cache"] = t1

        stats["frames"] += 1
        stats["ms_write_total"] += (t1 - t0) * 1000

        if t1 - stats["last_log"] >= 5.0:
            dt = t1 - stats["last_log"]
            n = stats["frames"] or 1
            log.info("evdi->ffmpeg: %d sent, %d dropped in %.1fs "
                     "(%.1f fps sent); write %.1f ms/f [mode=%s]",
                     stats["frames"], stats["dropped"], dt,
                     stats["frames"] / dt,
                     stats["ms_write_total"] / n,
                     encoder.mode["label"])
            stats.update(frames=0, dropped=0, last_log=t1,
                         ms_write_total=0.0)

    card.mode_changed_handler = on_mode_changed
    card.acquire_framebuffer_handler = on_acquire_framebuffer

    sku_area = FRAME_W * FRAME_H
    sku_pclk = sku_area * 60
    card.connect(edid, len(edid), sku_area, sku_pclk)
    log.info("evdi connected; pumping events (Ctrl-C to stop)")

    # Background launches (sh start.sh & / CI scripts) get SIGINT ignored by
    # the shell; route SIGTERM into the same graceful-shutdown path.
    def _sigterm_to_interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)

    try:
        while not shutdown.is_set():
            card.handle_events(500)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        shutdown.set()
        try: card.disconnect()
        except Exception: pass
        try: card.close()
        except Exception: pass
        try: encoder.stop()
        except Exception: pass
        sink.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--bind", default="127.0.0.1",
                   help="address to listen on. default 127.0.0.1, which is "
                        "all 'adb reverse' needs; the stream is "
                        "unauthenticated, so only widen this (e.g. 0.0.0.0) "
                        "on a network you trust.")
    p.add_argument("--vaapi-device", default=VAAPI_DEV)
    p.add_argument("--edid", default=DEFAULT_EDID)
    p.add_argument("--keyframe-interval-sec", type=float, default=0.5,
                   dest="keyframe_interval_sec",
                   help="Wall-clock seconds between forced IDRs (default "
                        "0.5). Bounds how long the receiver stays corrupted "
                        "after a lost access unit, independent of fps.")
    p.add_argument("--csc", choices=["gpu", "cpu"], default="gpu",
                   help="BGR->NV12 colour-space conversion location. "
                        "gpu (default): scale_vaapi on AMD VCN (~10pp CPU "
                        "lower than CPU CSC; emits BT.709 limited SPS tags). "
                        "cpu: libswscale on CPU (no SPS colour tags; safer "
                        "if a future driver bug breaks GPU CSC).")
    # Rate control + quality (AMD VAAPI driver supports CQP/CBR/VBR; ICQ is
    # listed by ffmpeg generically but rejected at runtime on this driver).
    p.add_argument("--rc-mode", choices=["VBR", "CQP", "CBR"], default="VBR",
                   help="rate control: VBR (default, average + cap; best "
                        "balance for desktop+video on a bounded pipe) / "
                        "CQP (constant QP, uncapped) / CBR (strict, legacy)")
    p.add_argument("--qp", type=int, default=22,
                   help="CQP constant quantisation parameter (0-52, lower=better). "
                        "default 22")
    p.add_argument("--quality", type=int, default=0,
                   help="VAAPI encoder effort: 0=best/slowest, higher=faster. "
                        "default 0")
    p.add_argument("--async-depth", dest="async_depth", type=int, default=1,
                   help="VAAPI encoder pipeline depth. 1 (default) minimises "
                        "latency; raise to 2+ for max throughput headroom.")
    p.add_argument("--bitrate", default="20M",
                   help="VBR average bitrate target / CBR bitrate. default 20M")
    p.add_argument("--maxrate", default="35M",
                   help="VBR upper cap; ignored by CQP/CBR. default 35M")
    p.add_argument("--bufsize", default="35M",
                   help="rate-buffer size. default 35M")
    # Adaptive desktop/video mode switching
    p.add_argument("--no-auto-mode", action="store_true",
                   help="disable auto-detection of video content; stay in "
                        "desktop mode forever (60 fps, args bitrate)")
    p.add_argument("--video-threshold", type=float, default=None,
                   help="dirty-coverage ratio above which a streak counts "
                        "toward switching to video mode. default 0.80")
    p.add_argument("--desktop-threshold", type=float, default=None,
                   help="dirty-coverage ratio below which a streak counts "
                        "toward switching back to desktop mode. default 0.40")
    return p


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main())
