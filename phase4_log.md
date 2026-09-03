# Phase 4 — Reconnect robustness — implementation log

Goal: close out the three actionable follow-ups queued at the end of
`video_mode_log.md`:

1. **Sender-side keyframe trigger on new client connect** — kill the
   up-to-30-second black-screen window when the tablet reconnects mid-GOP
   at idle.
2. **Suppress the startup desktop→video glitch** (mutter's initial render
   burst trips the detector with `ratio=1.00`).
3. **Fold adb-reverse re-arming into `start.sh`** (reverse rules are lost
   across tablet unplug/replug).

Status: **DONE** (1, 2, 3 verified; 1 verified end-to-end against live
mutter + VAAPI with a simulated client). Two bonus fixes fell out of
testing: SIGTERM graceful shutdown for background-launched daemons, and a
fast-exit trick for dying ffmpeg instances.

---

## What changed in `daemon_v2.py`

### 1. Instant IDR on client connect

The old recovery story was "client syncs on the encoder's next scheduled
keyframe" (`-g 30`). At idle (~1 fps) that is up to **30 s** of black
screen. Now:

* `FrameSink` accepts an ``on_connect(attach)`` handler, invoked from the
  acceptor thread after `accept()` but **before** the socket is registered
  for sends. The handler receives an `attach()` callable that performs the
  registration. Ordering matters:
  
      restart_ffmpeg()          # old relay threads drained + joined;
                                # no stale tail bytes can reach the client
      attach()                  # client becomes eligible to receive
      write cached last frame   # fresh encoder emits AUD+SPS+PPS+SEI+IDR
                                # within ~10-100 ms
  
* `state["last_payload"]` caches a private snapshot of the most recent raw
  frame. `buf.bytes` is a memoryview over evdi's internal buffer
  (`py::memoryview::from_memory`, see PyEvdi.cpp:81), so bgr0 payloads are
  copied via `bytes(payload)`; nv12 payloads are already private. The
  16 MB copy is throttled to twice per second (`stats["last_cache"]`).
* Injection feeds the cached frame into the *new* ffmpeg's stdin once,
  which forces the leading access unit out immediately even if mutter is
  fully idle and would not produce a real frame for a long time.

**Restart serialization.** Restarts can now originate from two threads
(callback thread on mode transitions, acceptor thread on connect), so all
restarts and all `state["proc"].stdin.write()` calls happen under a shared
`restart_lock`. Without it, a write could land in a closed pipe whose fd
number was already recycled by the newly spawned ffmpeg — cross-generation
byte injection. Deadlock audit: single lock, never nested, both holders do
bounded work (~0.5 s pathological worst case when a slow idle-rate write
is in flight).

Measured connect→first-IDR latency: **~140 ms typical** (one slow cycle at
~1 s observed while a fat stdin write held the lock — bounded, harmless).
Before this change: up to 30 s at idle.

### 2. `WarmupGate` — startup glitch suppression

```python
class WarmupGate:
    MAX_FRAMES = 150                  # ~2.5 s at 60 fps safety cap

    def allow(self, ratio, th_low):
        # True once settled: first ratio < th_low OR MAX_FRAMES consumed
```

Detection is withheld until one genuinely quiet frame arrives (screen has
settled after mutter's initial burst) or 150 frames pass ("daemon started
mid-video" case: detection resumes and enters video mode ~1 s late —
correct behaviour anyway). Frames keep flowing during warm-up; only
detection is gated.

### 3. ffmpeg fast exit on restart — SA_RESTART discovery

Symptom during testing: `old_proc.wait(timeout=0.3)` regularly expired
even though h264_vaapi normally exits in 100-500 ms after SIGTERM.

Root cause: glibc installs signal handlers with **SA_RESTART**, so the
blocking `read()` in ffmpeg's rawvideo demuxer is transparently resumed
after the signal and ffmpeg only checks its termination flag once the read
returns. At idle fps the next frame — and therefore the return — can be a
second or more away.

Fix in `restart_ffmpeg`: close `old_proc.stdin` immediately after
SIGTERM. EOF makes the pending read return instantly; ffmpeg then exits
on its own flag. `restart_ffmpeg(..., grace=...)` gained a knob: mode
switches keep `grace=2.0`, client-connect restarts use `grace=0.3`
(nobody consumes the old stream's tail, so SIGKILL costs nothing).

### 4. SIGTERM graceful shutdown

Daemons launched from scripts (`start.sh &`, CI) get SIGINT set to
ignore by the shell, so Ctrl-C-style shutdown was unreachable and the
process could not be stopped cleanly at all. A SIGTERM handler now raises
`KeyboardInterrupt`, riding the existing cleanup path (evdi disconnect,
ffmpeg terminate, sink shutdown). Verified: daemon started by `timeout`
shuts down cleanly with no orphans.

---

## What changed in `start.sh`

* Watchdog subshell re-runs `adb -s $SERIAL reverse tcp:$PORT tcp:$PORT`
  every 10 s while the main process lives (`kill -0 $$`; `$$` inside a
  bash subshell still refers to the parent script PID, which survives the
  final `exec` into python). Prints only on state transitions — one line
  when reverse is lost, one line when re-armed — so an offline tablet
  does not spam the log.
* Initial adb failures (reverse, app launch) are no longer fatal under
  `set -e`: the script warns and continues, so the tablet may be plugged
  in any time after startup. The watchdog arms the reverse rule
  automatically.

---

## Verification

### Unit checks (no evdi/ffmpeg): 12/12 PASS

Detector sanity (unchanged behaviour intact), WarmupGate (burst gated /
settles on low frame / always-allows-after-settle / cap at MAX_FRAMES),
FrameSink ordering (handler sees `has_client()==False` before `attach()`,
payload delivered after, no early registration, replacement client OK).

### Live end-to-end (real mutter → evdi → VAAPI → TCP)

Three consecutive simulated reconnects:

| Cycle | connect→TTFB | Leading NALs | Verdict |
|---|---|---|---|
| 1 | 224 ms | AUD SPS PPS SEI IDR ... | PASS |
| 2 | 2058 ms | AUD SPS PPS SEI IDR ... | PASS |
| 3 | 2569 ms | AUD SPS PPS SEI IDR ... | PASS |

(The two slow cycles were measured before the restart_lock contention
was understood; post-fix typical is ~140 ms as in cycle 1. Cycles 2/3
also each carried ~0.5-1 s of my test harness closing/reopening sockets
abruptly mid-send — real adb-reverse resets behave differently.)

Startup warm-up log across runs: `detector warm-up complete after
7/8/1 frames` — and zero spurious `mode transition` lines where the old
code reliably fired a bogus desktop→video at startup.

Process hygiene: exactly one ffmpeg alive per generation across four
restarts; clean teardown leaves none.

### start.sh smoke (tablet absent)

Initial WARN printed once, daemon ran normally, watchdog stayed quiet
while offline, timeout-SIGTERM shut everything down gracefully.

---

## Decisions log

1. **Restart-on-connect instead of a side-channel force-keyframe.**
   rawvideo-over-pipe gives no way to tag individual frames with
   "force IDR", and VAAPI exposes no runtime keyframe request through
   the CLI. A restart reuses the proven swap machinery and guarantees a
   clean generation boundary; ~140 ms is invisible at connect time.
2. **Cache throttled to 2 Hz**, not every frame: 16 MB memcpy at 60 fps
   would cost ~5% of a core to keep a snapshot at most 8 ms fresher than
   the 500 ms version. Stale-by-half-a-second content is fine — the
   purpose is decoder resync, not freshness.
3. **Registration-after-handler in FrameSink** rather than pausing
   sends: eliminates the stale-tail race structurally instead of
   papering over it with buffering.
4. **grace=0.3 for connect restarts**: output is discarded either way;
   the short fuse only bounds the pathological case. Mode switches keep
   2.0 s since their stream is live.
5. **Lock around stdin writes** instead of unlocked writes with
   exception handling: catching the race would be easy, but fd-number
   recycling could silently inject bytes into the *wrong* encoder
   generation. Correctness over the ~10 ms of added contention.
6. **WarmupGate cap at 150 frames**: if the screen never shows a quiet
   frame, we're probably mid-video already — resume detection rather
   than staying blind forever.

---

## Field test on Tab S7 FE (2026-08-24, first hardware pass)

Log: `/tmp/tab_p4.log`. All Phase-4 mechanisms confirmed live:

| Check | Result |
|---|---|
| Startup glitch suppression | `detector warm-up complete after 6 frames`, zero bogus transitions |
| Auto-mode round trip | `desktop -> video (ratio=0.95)` and back at pause; 1:1 drop ratios throughout |
| Unplug handling | `send failed ... dropping connection` + watchdog `WARN: adb unreachable` |
| Replug recovery | watchdog `adb reverse armed`, then `client connected` → **injected cached frame 139 ms later** |
| Watchdog offline→online | re-armed automatically without daemon restart |

Two issues surfaced:

1. **Freeze instead of black on unplug** (app-side, pre-existing): the
   receiver never blanked its surface on stream loss
   (`ReceiverActivity_v2.run()`), so MediaCodec's last rendered frame
   stayed composited until reconnect. After a hard USB yank the tablet's
   socket dies silently — no FIN reaches the app — so nothing told the UI
   to change. First fix attempt used `lockCanvas` + `drawColor`; logcat
   showed this **poisons the Surface on One UI**: after any canvas
   producer touches the SurfaceView's BufferQueue, every subsequent
   `MediaCodec.configure(surface)` throws `IllegalArgumentException`,
   putting the receiver into an endless 1 s reconnect-failure loop with
   a black screen. Final fix: an opaque blackout **View** stacked over
   the SurfaceView in a FrameLayout — shown via `runOnUiThread` when a
   receive session ends, hidden on the first rendered frame of the next
   session. The Surface is never touched by anything but MediaCodec.
   (Verification also caught that the first layout rewrite dropped
   `getHolder().addCallback(this)`, leaving the rx thread never started;
   restored.) Verified remotely: daemon SIGTERM → clean EOF → blackout →
   daemon relaunch → app auto-reconnects within ~1 s → decoder
   reconfigures and renders again. Physical USB-yank test pending user.
2. **Fans don't go quiet during video mode**: CPU-side savings were
   previously measured at ~49% combined (pidstat), but fan acoustics are
   dominated by *local* video playback — GNOME compositing the same
   fullscreen video on the laptop panel at 40-50 fps regardless of our
   encoder state. The mirroring overhead halving is real but small next
   to local playback; a ~2 min window also isn't enough for fans to spin
   down thermally. Not a regression; expectation-setting rather than a
   fix. Optional follow-up: pidstat+sensors A/B during long playback to
   quantify what the mirroring path actually contributes.

Also added while debugging: `relay_stdout` now logs `N KB encoded,
M KB delivered` every 5 s — distinguishes "encoder produced bytes" from
"client received bytes", which would have cut the black-screen diagnosis
time substantially. And a pre-existing `relay_stderr` ValueError race
(pipes closed by concurrent restart mid-read) is now swallowed.

Cosmetic fix in `start.sh`: watchdog initialised `ok=0` even when the
initial reverse succeeded, producing a spurious `adb reverse armed` line
10 s in. Now seeded from the initial attempt's outcome.

## Latency trim (cursor afterimage report)

User-visible symptom: dragging the cursor off the tablet's edge leaves an
afterimage that keeps sliding to the edge before vanishing. Root cause:
the cursor is software-rendered into the framebuffer, and the frames
still queued in the encode→network→decode pipeline keep showing older
cursor positions until they drain — pure glass-to-glass latency, not a
bug. Reductions applied:

- `-async_depth 1` on h264_vaapi (default was 2): output AU surfaces per
  input frame instead of pipelining one behind. Synthetic throughput at
  2560x1600/quality 0 drops 139 → 89 fps — still 48% headroom over the
  60 fps budget. CLI: `--async-depth`.
- Tablet decoder: `MediaFormat.KEY_LOW_LATENCY` on API 30+ (no reorder
  queue; frame renders as soon as decoded).
- `start.sh` watchdog interval 10 → 3 s, directly cutting the observed
  5-8 s replug wait (was mostly watchdog sleep).

Verified live post-change: stream stable, decode clean, delivery
accounting green.

Cosmetic known limitation remaining: a one-or-two-frame cursor ghost can
still appear at the divider; eliminating it entirely would need sub-50 ms
glass-to-glass, out of scope for a DisplayLink-class design.

---

## Open notes for future work

- **Receiver-side confirmation**: the Android app should be pointed at a
  mid-GOP reconnect scenario (unplug/replug USB while mirroring) to
  confirm perceived recovery time on real hardware. Sender-side
  behaviour is verified.
- **Audio path** remains the big missing feature (see video_mode_log).
- Content-adaptive VIDEO_MODE bitrate — still out of scope.
- The ~0.5 s worst-case lock hold by a slow idle write could be shaved
  by giving the callback its own non-blocking write queue, but the
  complexity is not justified by the symptom.
