#!/usr/bin/env python3
"""
Phase 3v2 daemon: evdi -> VAAPI H.264 -> TCP -> tablet MediaCodec.

Layout:
  - Main thread pumps PyEvdi events. The acquire_framebuffer callback runs
    here: numpy XRGB->NV12, then write the NV12 raw frame to ffmpeg's stdin.
  - ffmpeg subprocess: rawvideo NV12 in -> h264_vaapi out, Annex B on stdout.
  - "ffmpeg-stdout" thread: reads encoded chunks and forwards them to the
    current client socket.
  - "acceptor" thread: TCP listener on 7070; holds at most one client.

Backpressure: writes to ffmpeg.stdin block when VAAPI/disk can't keep up, which
naturally throttles PyEvdi event processing. Encoded output dropped when no
client is attached. On (re)connect the daemon restarts ffmpeg and injects the
most recent frame into the fresh encoder, so the client's first bytes are a
SPS+PPS+IDR access unit — no waiting for the next scheduled keyframe.
"""
import argparse
import fcntl
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import numpy as np

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
# TCP sink (same shape as v1 — single client, auto-reaccept)
# ---------------------------------------------------------------------------

class FrameSink:
    """Single-client TCP sink.

    ``on_connect``, when supplied, is invoked from the acceptor thread after
    accept() but BEFORE the new socket is registered for sends. It receives
    an ``attach`` callable; calling it registers the client. This ordering
    lets the handler swap ffmpeg first, so a fresh client never receives
    tail bytes of the previous encoder's stream:

        restart_ffmpeg()   # old relays drained + joined
        attach()           # client now eligible to receive
        inject cached frame# forces immediate SPS+PPS+IDR out of new ffmpeg
    """

    def __init__(self, port: int, on_connect=None):
        self.port = port
        self._on_connect = on_connect
        self._lock = threading.Lock()
        self._conn = None
        self._dead = threading.Event()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="acceptor",
                                        daemon=True)
        self._thread.start()

    def _register(self, conn) -> None:
        with self._lock:
            self._conn = conn
            self._dead.clear()

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

    def send(self, data: bytes) -> bool:
        with self._lock:
            conn = self._conn
        if conn is None:
            return False
        try:
            conn.sendall(data)
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


# ---------------------------------------------------------------------------
# ffmpeg subprocess plumbing
# ---------------------------------------------------------------------------

def build_ffmpeg_cmd(args, mode: dict) -> list:
    """rawvideo in (NV12 or BGR0) -> h264 Annex B out, VAAPI encode.

    ``mode`` is one of DESKTOP_MODE / VIDEO_MODE and supplies fps, bitrate,
    maxrate, bufsize. The rest of the encoder configuration (profile, GOP,
    AUD, colour metadata, CSC location) is shared between modes.
    """
    in_pix = args.pixel_format    # "nv12" or "bgr0"

    # CSC (BGR->NV12) location:
    #   --csc cpu (default, safe): libswscale on the CPU, ~10-15% of one core
    #     at 2560x1600@60. No explicit colour-metadata tags needed — the
    #     decoder's resolution-based BT.709 inference matches well enough
    #     (this is what v0 worked with).
    #   --csc gpu: VAAPI's scale_vaapi does CSC on-chip, freeing the CPU.
    #     Requires explicit BT.709 limited-range tagging on the output so the
    #     decoder reads back what the VPP block actually wrote.
    if args.csc == "gpu":
        vf = ("hwupload,scale_vaapi=format=nv12:"
              "out_color_matrix=bt709:out_range=limited")
    else:
        vf = "format=nv12,hwupload"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-init_hw_device", f"vaapi=va:{args.vaapi_device}",
        "-filter_hw_device", "va",
        # Wall-clock arrival timestamps for the rawvideo input. Without
        # this the demuxer stamps PTS by frame count (1/60 s each), which
        # silently turns time-based behaviour back into frame-count
        # behaviour at idle: the 0.5 s forced-IDR cadence would stretch to
        # ~30 real seconds at 1 fps, and VBR would misallocate bits. With
        # real timestamps both stay honest at any fps.
        "-use_wallclock_as_timestamps", "1",
        "-f", "rawvideo",
        "-pix_fmt", in_pix,
        "-s", f"{FRAME_W}x{FRAME_H}",
        "-r", str(mode["fps"]),
        "-i", "pipe:0",
        "-vf", vf,
        "-c:v", "h264_vaapi",
        "-profile:v", "high",
        "-level", "5.1",
        # Wall-clock keyframes, NOT a frame-count GOP: at mutter's idle fps
        # a -g 30 GOP stretches to ~30 s without an IDR, so any dropped or
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
        # Insert an Access Unit Delimiter NAL (type 9) at the start of every
        # frame so the receiver's AU-aware framing has clean boundaries.
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

    cmd += ["-f", "h264", "pipe:1"]
    return cmd


def relay_stdout(proc: subprocess.Popen, sink: FrameSink,
                 shutdown: threading.Event) -> None:
    """Drain ffmpeg's stdout and forward chunks to the current client."""
    fd = proc.stdout.fileno()
    CHUNK = 64 * 1024
    produced = 0                     # bytes out of the encoder
    delivered = 0                    # bytes actually handed to a client
    last_log = time.monotonic()
    while not shutdown.is_set():
        try:
            data = os.read(fd, CHUNK)
        except OSError:
            break
        if not data:
            break
        produced += len(data)
        if sink.has_client():
            if sink.send(data):
                delivered += len(data)
        now = time.monotonic()
        if now - last_log >= 5.0:
            log.info("relay: %d KB encoded, %d KB delivered in %.1fs",
                     produced // 1024, delivered // 1024, now - last_log)
            produced = 0
            delivered = 0
            last_log = now
    log.info("relay_stdout exiting (%d bytes delivered total)",
             delivered)


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


def _grow_pipe(fileobj, size: int = 1 << 20) -> None:
    """Grow a pipe buffer (default 64 KB -> up to 1 MB).

    A 16 MB frame crosses a 64 KB pipe in ~256 fill/drain round trips,
    each costing context switches — measured 9-23 ms per frame write.
    A 1 MB buffer cuts that to ~16 trips (~1-2 ms) with zero quality
    impact; the encoder keeps up at these rates, so the extra in-flight
    buffering never accumulates into delay."""
    try:
        with open("/proc/sys/fs/pipe-max-size") as fh:
            cap = int(fh.read().strip())
    except (OSError, ValueError):
        cap = size
    try:
        fcntl.fcntl(fileobj.fileno(), fcntl.F_SETPIPE_SZ, min(size, cap))
    except OSError as exc:
        log.warning("could not grow pipe buffer: %s", exc)


def _spawn_ffmpeg(args, mode: dict, sink, shutdown):
    """Start ffmpeg for ``mode`` and the two relay threads. Returns
    (proc, [stdout_thr, stderr_thr])."""
    cmd = build_ffmpeg_cmd(args, mode)
    log.info("ffmpeg cmd (%s, %d fps, %s/%s): %s",
             mode["label"], mode["fps"], mode["bitrate"], mode["maxrate"],
             " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    _grow_pipe(proc.stdin)
    _grow_pipe(proc.stdout)
    stdout_thr = threading.Thread(
        target=relay_stdout, args=(proc, sink, shutdown),
        name=f"ffmpeg-stdout/{mode['label']}", daemon=True)
    stderr_thr = threading.Thread(
        target=relay_stderr, args=(proc, shutdown),
        name=f"ffmpeg-stderr/{mode['label']}", daemon=True)
    stdout_thr.start()
    stderr_thr.start()
    return proc, [stdout_thr, stderr_thr]


def restart_ffmpeg(state: dict, args, new_mode: dict, sink, shutdown,
                   why: str = "auto-mode", grace: float = 2.0) -> None:
    """Atomically swap the ffmpeg subprocess to use ``new_mode``.

    SIGTERM the old ffmpeg (faster than stdin-EOF flush; we don't care about
    losing in-flight bytes because the new stream's leading SPS+PPS+IDR
    resyncs the tablet decoder). ``grace`` bounds how long we wait for a
    clean exit before SIGKILL: mode switches keep the 2 s default, while
    client-connect restarts pass a short grace since nobody is consuming
    the old stream's tail anyway and the AMD VAAPI process occasionally
    ignores SIGTERM for seconds. Join the old relay threads before starting
    new ones so we never get interleaved bytes from two ffmpegs racing into
    the same TCP socket.

    Updates state['proc'], state['mode'], state['threads'],
    state['frame_counter'] in place. Callers must hold ``restart_lock``
    (serialises mode-switch restarts on the callback thread against
    client-connect restarts on the acceptor thread). Called from the
    framebuffer callback, so a 50-200 ms hiccup is tolerable.
    """
    old_proc    = state["proc"]
    old_threads = state["threads"]
    old_label   = state["mode"]["label"]
    log.info("ffmpeg restart (%s): %s -> %s", why, old_label, new_mode["label"])

    try:
        old_proc.terminate()
    except (OSError, ProcessLookupError):
        pass
    # Unblock ffmpeg's demuxer read: glibc installs SIGTERM with SA_RESTART,
    # so a blocking read() is transparently resumed and the termination flag
    # is only noticed once the read returns. At idle fps that can stall the
    # exit for a second or more. Closing stdin forces an immediate EOF.
    try:
        if old_proc.stdin is not None:
            old_proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        old_proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.warning("old ffmpeg did not exit in %.1f s; killing", grace)
        try:
            old_proc.kill()
            old_proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # stdin is already closed (see above); close the read ends so the relay
    # threads' blocking reads see EOF and exit.
    for fd in (old_proc.stdout, old_proc.stderr):
        try:
            fd.close()
        except (OSError, ValueError):
            pass

    # Wait for the old relay threads to actually exit so they can't race the
    # new threads writing to the socket.
    for t in old_threads:
        t.join(timeout=2)

    new_proc, new_threads = _spawn_ffmpeg(args, new_mode, sink, shutdown)
    state["proc"]          = new_proc
    state["mode"]          = new_mode
    state["threads"]       = new_threads
    state["frame_counter"] = 0


# ---------------------------------------------------------------------------
# Pixel-format conversion (baseline: BT.601 in numpy)
# ---------------------------------------------------------------------------

def xrgb_to_nv12(view: memoryview, w: int, h: int) -> bytes:
    """XRGB8888 little-endian (memory order B,G,R,X) -> NV12 raw bytes.

    NV12 layout in memory:
        plane 0: Y, h*w bytes
        plane 1: UV interleaved Cb,Cr,Cb,Cr,..., (h/2)*w bytes
    Total size: h*w + h*w/2 = h*w*3/2.
    """
    src = np.frombuffer(view, dtype=np.uint8).reshape(h, w, 4)
    B = src[:, :, 0].astype(np.float32)
    G = src[:, :, 1].astype(np.float32)
    R = src[:, :, 2].astype(np.float32)
    Y  = (0.257 * R + 0.504 * G + 0.098 * B + 16
          ).clip(16, 235).astype(np.uint8)
    Cb = (-0.148 * R - 0.291 * G + 0.439 * B + 128
          ).clip(16, 240).astype(np.uint8)
    Cr = ( 0.439 * R - 0.368 * G - 0.071 * B + 128
          ).clip(16, 240).astype(np.uint8)
    # 4:2:0 chroma subsample (take top-left of each 2x2 block).
    Cb_sub = Cb[0::2, 0::2]
    Cr_sub = Cr[0::2, 0::2]
    uv = np.empty((h // 2, w), dtype=np.uint8)
    uv[:, 0::2] = Cb_sub
    uv[:, 1::2] = Cr_sub
    return Y.tobytes() + uv.tobytes()


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

    restart_lock = threading.Lock()
    shutdown = threading.Event()

    # ``last_payload`` holds a private snapshot of the most recent raw frame
    # (refreshed at most twice per second) so a newly connected tablet can be
    # handed an immediate keyframe instead of waiting for the next scheduled
    # IDR — which at idle fps can be tens of seconds away mid-GOP.
    state = {
        "proc":          None,
        "threads":       [],
        "mode":          DESKTOP_MODE,
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
        if state["proc"] is None:      # still starting up; nothing to swap
            attach()
            return
        try:
            with restart_lock:
                restart_ffmpeg(state, args, state["mode"], sink, shutdown,
                               why="client connect", grace=0.3)
                attach()
                payload = state["last_payload"]
                if payload is not None:
                    state["proc"].stdin.write(payload)
                    log.info("injected cached frame; new client has an "
                             "immediate IDR")
                else:
                    log.info("no cached frame yet; client syncs on next "
                             "evdi update")
        except Exception:
            log.exception("client-connect resync failed; registering client "
                          "without injection")
            attach()

    sink = FrameSink(args.port, on_connect=handle_client_connect)

    proc, threads = _spawn_ffmpeg(args, DESKTOP_MODE, sink, shutdown)
    state["proc"]    = proc
    state["threads"] = threads

    detector = MotionDetector(
        threshold_high=args.video_threshold,
        threshold_low=args.desktop_threshold,
    )
    warmup = WarmupGate()

    card = PyEvdi.Card(idx)
    log.info("opened evdi card %d", idx)

    stats = {"frames": 0, "dropped": 0, "last_log": time.monotonic(),
             "ms_conv_total": 0.0, "ms_write_total": 0.0,
             "last_cache": 0.0}

    def on_mode_changed(mode):
        log.info("mode_changed: %dx%d @ %d Hz, bpp=%d, pixel_format=0x%x",
                 mode.width, mode.height, mode.refresh_rate,
                 mode.bits_per_pixel, mode.pixel_format)

    use_bgr0 = args.pixel_format == "bgr0"
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
                if transition == "video" and state["mode"] is DESKTOP_MODE:
                    log.info("mode transition: desktop -> video "
                             "(ratio=%.2f, streak=%d)",
                             ratio, detector.last_streak)
                    with restart_lock:
                        restart_ffmpeg(state, args, VIDEO_MODE, sink,
                                       shutdown)
                elif transition == "desktop" and state["mode"] is VIDEO_MODE:
                    log.info("mode transition: video -> desktop "
                             "(ratio=%.2f, streak=%d)",
                             ratio, detector.last_streak)
                    with restart_lock:
                        restart_ffmpeg(state, args, DESKTOP_MODE, sink,
                                       shutdown)

        # 2) Frame dropping in video mode: keep every other frame so the
        # encoder gets ~30 fps. (pyevdi exposes no explicit "release buffer"
        # method on the Buffer object; returning is equivalent.)
        state["frame_counter"] += 1
        if state["mode"] is VIDEO_MODE and state["frame_counter"] % 2 != 0:
            stats["dropped"] += 1
            return

        # 3) Send the frame to ffmpeg.
        t0 = time.monotonic()
        try:
            if use_bgr0:
                # Zero-cost: evdi's buffer is already XRGB8888 LE = BGRX in
                # memory, which ffmpeg's "bgr0" pix_fmt accepts verbatim.
                payload = buf.bytes
            else:
                payload = xrgb_to_nv12(buf.bytes, buf.width, buf.height)
        except Exception:
            log.exception("pixel conversion failed")
            return
        t1 = time.monotonic()
        try:
            # Lock serialises against restarts: prevents writing into the
            # old ffmpeg's stdin after a swap closed it (ValueError) and
            # keeps the write inside one encoder generation.
            with restart_lock:
                state["proc"].stdin.write(payload)
        except (BrokenPipeError, OSError, ValueError) as exc:
            log.error("ffmpeg stdin closed: %s", exc)
            shutdown.set()
            return
        t2 = time.monotonic()

        # Refresh the connect-time IDR snapshot. buf.bytes aliases evdi's
        # internal buffer, so bgr0 payloads must be copied out; nv12
        # payloads are already private bytes. Throttle the 16 MB copy to
        # twice per second.
        if use_bgr0:
            if t2 - stats["last_cache"] >= 0.5:
                state["last_payload"] = bytes(payload)
                stats["last_cache"] = t2
        else:
            state["last_payload"] = payload

        stats["frames"] += 1
        stats["ms_conv_total"]  += (t1 - t0) * 1000
        stats["ms_write_total"] += (t2 - t1) * 1000

        now = time.monotonic()
        if now - stats["last_log"] >= 5.0:
            dt = now - stats["last_log"]
            n = stats["frames"] or 1
            log.info("evdi->ffmpeg: %d sent, %d dropped in %.1fs "
                     "(%.1f fps sent); conv %.1f ms/f, write %.1f ms/f "
                     "[mode=%s]",
                     stats["frames"], stats["dropped"], dt,
                     stats["frames"] / dt,
                     stats["ms_conv_total"] / n,
                     stats["ms_write_total"] / n,
                     state["mode"]["label"])
            stats.update(frames=0, dropped=0, last_log=now,
                         ms_conv_total=0.0, ms_write_total=0.0)

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
        cur = state["proc"]
        try: cur.stdin.close()
        except Exception: pass
        try:
            cur.terminate()
            cur.wait(timeout=2)
        except Exception:
            try: cur.kill()
            except Exception: pass
        sink.shutdown()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--vaapi-device", default=VAAPI_DEV)
    p.add_argument("--edid", default=DEFAULT_EDID)
    p.add_argument("--keyframe-interval-sec", type=float, default=0.5,
                   dest="keyframe_interval_sec",
                   help="Wall-clock seconds between forced IDRs (default "
                        "0.5). Bounds how long the receiver stays corrupted "
                        "after a lost access unit, independent of fps.")
    p.add_argument("--pixel-format", choices=["bgr0", "nv12"], default="bgr0",
                   help="bgr0: write evdi's buffer raw (default, ~10x faster); "
                        "nv12: pre-convert in numpy (slow baseline)")
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
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
