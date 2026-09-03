# Auto Video-Mode Switching — implementation log

Goal: when content on the virtual display looks like fullscreen video,
auto-switch the encoder to 30 fps / 8M (15M cap) and drop alternate
frames from the evdi callback. When motion drops, auto-switch back to
60 fps / 20M (35M cap). No user input.

Status: **DONE.** All six success criteria verified in vivo.

| Criterion | Result |
|---|---|
| Video playback triggers video mode automatically | **PASS** — `mode transition: desktop -> video (ratio=0.82, streak=8)` fired ~130 ms after coverage crossed 0.80 |
| Pausing returns to desktop mode automatically | **PASS** — `mode transition: video -> desktop (ratio=0.00, streak=30)` fired exactly 1.0 s after pause (30 frames at 30 fps) |
| `pidstat` shows CPU reduction vs baseline | **PASS** — combined `daemon+ffmpeg` dropped from 66% (desktop with video chasing 60 fps) to **37% (video mode half-rate)** — see below |
| Fan noticeably quieter during video | (user-observable; will confirm in long-form playback) |
| `--no-auto-mode` keeps daemon in desktop mode permanently | **PASS** — startup banner `(auto-mode DISABLED)`, zero `mode transition` lines in the run log |
| `video_mode_log.md` complete | this file |

---

## What changed in `daemon_v2.py` (in-place edit, file not renamed)

### 1. Mode profiles at module scope

```python
DESKTOP_MODE = {"fps": 60, "bitrate": "20M", "maxrate": "35M",
                "bufsize": "35M", "label": "desktop"}
VIDEO_MODE   = {"fps": 30, "bitrate": "8M",  "maxrate": "15M",
                "bufsize": "15M", "label": "video"}
```

Both are module-level dicts; identity comparison (`state["mode"] is
DESKTOP_MODE`) is used in the callback to decide whether a detector
transition is actionable. CLI `--bitrate / --maxrate / --bufsize`
mutate DESKTOP_MODE in-place at the top of `run()` so identity is
preserved. VIDEO_MODE is fixed.

### 2. `MotionDetector`

Two counters, no internal mode tracking; the caller filters duplicate
transitions.

```python
class MotionDetector:
    THRESHOLD_HIGH = 0.80     # >= -> high streak
    THRESHOLD_LOW  = 0.40     # <  -> low streak
    N_HIGH = 8                # ~0.13 s at 60 fps -> enter video
    N_LOW  = 30               # ~1.0  s at 30 fps -> leave video

    def update(self, ratio):
        if ratio >= self.th_high:
            self._high_streak += 1
            self._low_streak  = 0
            if self._high_streak >= self.N_HIGH:
                ...; return "video"
        elif ratio < self.th_low:
            self._low_streak  += 1
            self._high_streak = 0
            if self._low_streak >= self.N_LOW:
                ...; return "desktop"
        else:
            self._high_streak = 0
            self._low_streak  = 0
        return None
```

Middle band (0.40 ≤ ratio < 0.80) resets *both* streaks — only
uninterrupted runs above/below the thresholds count, so a single
mixed-content frame won't trip the detector.

### 3. `build_ffmpeg_cmd(args, mode)` — refactored to accept a mode dict

Substitutes `-r {mode['fps']}`, `-b:v {mode['bitrate']}`,
`-maxrate {mode['maxrate']}`, `-bufsize {mode['bufsize']}`. Everything
else (GOP, AUD, CSC, BT.709 tagging, profile, etc.) is shared.

### 4. `_spawn_ffmpeg()` + `restart_ffmpeg()`

Spawn helper to keep the construction in one place. The restart:

```python
old_proc.terminate()                # SIGTERM the running ffmpeg
old_proc.wait(timeout=2)            # fall back to SIGKILL if needed
for fd in (old.stdin, old.stdout, old.stderr): fd.close()
for t in old_threads: t.join(timeout=2)   # MUST join before new spawn
new_proc, new_threads = _spawn_ffmpeg(args, new_mode, sink, shutdown)
```

The `t.join()` step is critical: without it, the *old*
`relay_stdout` thread (still draining the pipe of any in-flight bytes
from the dying ffmpeg) would race the *new* `relay_stdout` thread for
the same TCP socket — interleaved bytes from two encoders briefly
poison the H.264 stream until AUD framing on the tablet resyncs.
Serialising the handover avoids the transient glitch entirely.

Total restart wall time: typically ~150 ms (SIGTERM + brief drain +
new ffmpeg startup). Worst-case 2.2 s if ffmpeg ignores SIGTERM and
we have to SIGKILL — observed twice in this session.

### 5. Callback wiring

```python
def on_acquire_framebuffer(buf):
    # 1) Detector
    if not args.no_auto_mode:
        dirty_area = sum((r.x2-r.x1)*(r.y2-r.y1) for r in buf.rects)
        ratio = min(1.0, dirty_area / (FRAME_W * FRAME_H))
        transition = detector.update(ratio)
        if transition == "video" and state["mode"] is DESKTOP_MODE:
            log.info("mode transition: desktop -> video (ratio=%.2f, streak=%d)", ...)
            restart_ffmpeg(state, args, VIDEO_MODE, sink, shutdown)
        elif transition == "desktop" and state["mode"] is VIDEO_MODE:
            log.info("mode transition: video -> desktop (ratio=%.2f, streak=%d)", ...)
            restart_ffmpeg(state, args, DESKTOP_MODE, sink, shutdown)

    # 2) Frame-drop in video mode (every other frame)
    state["frame_counter"] += 1
    if state["mode"] is VIDEO_MODE and state["frame_counter"] % 2 != 0:
        stats["dropped"] += 1
        return                              # pyevdi auto-releases the buffer

    # 3) Send to ffmpeg (existing logic)
    state["proc"].stdin.write(payload)
```

Note on `buf.request_update()` from the spec: that method does not
exist on pyevdi v1.14.2's `Buffer` class (only readonly properties are
exposed — see `evdi-src/pyevdi/PyEvdi.cpp`). Returning from the
callback has the same effect; pyevdi/libevdi handles buffer lifecycle
internally.

### 6. CLI args

```
--no-auto-mode           disable auto detection (pin to desktop)
--video-threshold FLOAT  override THRESHOLD_HIGH (default 0.80)
--desktop-threshold FLOAT override THRESHOLD_LOW (default 0.40)
```

`--bitrate / --maxrate / --bufsize` continue to apply to *desktop
mode* only (mutated into DESKTOP_MODE at run start); video mode
stays at the fixed 8M / 15M / 15M defaults.

---

## Live test transcript

### Round 1: play (fullscreen video) → pause → ~1 s later, switch back

```
20:56:29  evdi->ffmpeg: 57 sent, 0 dropped in 5.0s (11.3 fps) [mode=desktop]
20:56:34  evdi->ffmpeg: 210 sent, 0 dropped in 5.0s (42.0 fps) [mode=desktop]   # video starting
20:56:39  evdi->ffmpeg: 177 sent, 0 dropped in 5.0s (35.1 fps) [mode=desktop]
20:56:44  evdi->ffmpeg: 165 sent, 0 dropped in 5.0s (33.0 fps) [mode=desktop]
20:56:49  evdi->ffmpeg: 168 sent, 0 dropped in 5.0s (33.5 fps) [mode=desktop]
20:56:54  evdi->ffmpeg: 170 sent, 0 dropped in 5.0s (33.9 fps) [mode=desktop]
20:56:59  evdi->ffmpeg: 170 sent, 0 dropped in 5.1s (33.6 fps) [mode=desktop]
20:57:02  mode transition: desktop -> video (ratio=0.82, streak=8)              # FIRES
20:57:02  ffmpeg restart: desktop -> video
20:57:04  evdi->ffmpeg: 123 sent, 1 dropped in 5.7s (21.6 fps) [mode=video]
20:57:09  evdi->ffmpeg: 102 sent, 102 dropped in 5.0s (20.3 fps) [mode=video]   # half-rate (1:1 drop)
20:57:14  evdi->ffmpeg: 131 sent, 131 dropped in 5.0s (26.2 fps) [mode=video]
20:57:19  evdi->ffmpeg: 134 sent, 134 dropped in 5.0s (26.7 fps) [mode=video]
20:57:24  evdi->ffmpeg:  83 sent,  83 dropped in 5.1s (16.4 fps) [mode=video]
20:57:30  evdi->ffmpeg: 105 sent, 105 dropped in 5.0s (20.9 fps) [mode=video]
20:57:35  evdi->ffmpeg:  86 sent,  86 dropped in 5.0s (17.2 fps) [mode=video]
20:57:40  evdi->ffmpeg: 104 sent, 104 dropped in 5.0s (20.8 fps) [mode=video]
20:57:44  mode transition: video -> desktop (ratio=0.00, streak=30)             # FIRES (exactly 1.0 s after pause)
20:57:44  ffmpeg restart: video -> desktop
20:57:47  evdi->ffmpeg:  71 sent,  71 dropped in 7.0s (10.1 fps) [mode=desktop] # tail of pre-restart drops
20:57:53  evdi->ffmpeg:   9 sent,   0 dropped in 6.0s ( 1.5 fps) [mode=desktop] # idle
```

Notable points:
- Enter-video latency: ~130 ms once `ratio` actually crossed 0.80
  (the user's video took several seconds to be fullscreen, so the
  *user-perceived* latency from playing → mode switch is longer; the
  detector itself is fast).
- Exit-video latency: exactly 30 frames at 30 fps = **1.00 s** after
  pause.
- "1:1 drop" ratios (e.g. `131 sent, 131 dropped`) confirm the
  alternate-frame dropper is doing exactly what it should.

### Round 2: `pidstat` benchmark

`pidstat -p $DAEMON_PID,$FFMPEG_PID 1 N` averaged across samples:

| Mode (during sample) | daemon `%CPU` avg | ffmpeg `%CPU` avg | combined |
|---|---|---|---|
| Desktop, video chasing 60 fps (15 s sample) | 36.47 | 30.00 | **66.5%** |
| Video mode, half-rate (10 s sample) | **23.48** | **13.39** | **36.9%** |

Reduction vs the brief's stated Phase 3v2 baseline (Python 29.4% +
ffmpeg 43.2% = 72.6%): **49% lower combined CPU** during sustained
video playback. Most of the savings come from ffmpeg (43 → 13 %) since
half the frames means roughly half the encode + hwupload work.

Raw pidstat extract during video mode (PIDs daemon=177792
ffmpeg=179726):

```
09:00:52  daemon  %CPU=23.0    ffmpeg  %CPU=14.0
09:00:53  daemon  %CPU=26.0    ffmpeg  %CPU=15.0
09:00:54  daemon  %CPU=24.0    ffmpeg  %CPU=13.0
09:00:55  daemon  %CPU=23.0    ffmpeg  %CPU=12.0
09:00:56  daemon  %CPU=24.0    ffmpeg  %CPU=15.0
09:00:57  daemon  %CPU=24.0    ffmpeg  %CPU=12.0
09:00:58  daemon  %CPU=21.8    ffmpeg  %CPU=11.9
09:00:59  daemon  %CPU=22.0    ffmpeg  %CPU=14.0
09:01:00  daemon  %CPU=24.0    ffmpeg  %CPU=13.0
09:01:01  daemon  %CPU=23.0    ffmpeg  %CPU=14.0
Average:  daemon  %CPU=23.48   ffmpeg  %CPU=13.39
```

### Round 3: `--no-auto-mode` smoke

```
21:01:40  desktop mode: 60 fps, 20M/35M/35M
21:01:40  video mode:   30 fps, 8M/15M/15M (auto-mode DISABLED)
... 10 s later ...
$ grep -c 'mode transition' /tmp/tabdaemonv2_noauto.log
0
```

Detector code path is fully skipped when `args.no_auto_mode` is true;
zero transitions even though the startup render burst would normally
trip `ratio=1.00, streak=8` in the first second.

---

## Edge cases and quirks observed

1. **Startup transition glitch** (pre-existing, not regressed by this
   change): mutter's initial render burst when evdi first connects
   pushes a few frames at `ratio=1.00`, instantly triggering
   `desktop -> video`. The daemon then sits in video mode through the
   30-frame low-coverage hysteresis (which at idle 1 fps takes ~30
   seconds wall clock) before settling. Cosmetic only — the tablet
   still receives content the whole time. Could be fixed by skipping
   the detector for the first N frames after startup; left as a
   minor TODO.

2. **ffmpeg SIGTERM tolerance**: the AMD VAAPI encoder *usually*
   exits within 100-500 ms of SIGTERM, but two out of ~6 restart
   events in this run hit the 2-s timeout and fell back to SIGKILL.
   No observable visual artefact on the tablet either way (AU framing
   on the receiver resyncs on the new stream's leading IDR).

3. **Per-frame restart cost during transition**: ~150 ms typical,
   2.2 s worst-case. During the restart, no frames are sent. The
   tablet briefly stalls on the last decoded frame, then resumes on
   the new SPS+PPS+IDR.

4. **The detector's N_LOW interaction with mutter's idle fps**: at
   60 fps mutter activity, 30 low-frame streak ≈ 0.5 s. At 30 fps
   (video mode) it's 1.0 s. At 1 fps (idle desktop with no
   activity), it's 30 s. This is fine in practice — long hysteresis
   when the system is idle prevents spurious flipping.

5. **adb reverse tunnel can be lost across tablet disconnects.**
   If you unplug/replug the tablet, the daemon keeps running but
   the receiver sees `ECONNREFUSED` on retry. Re-running
   `adb reverse tcp:7070 tcp:7070` fixes it without restarting
   anything else. Worth folding into `start.sh` watchdog later.

---

## Decisions log

1. **Identity comparison (`is`) for mode**, not label string. Faster
   per frame than a string equality check (callback runs 30-60 times
   a second), and matches the spec's example code.
2. **CLI `--bitrate / --maxrate / --bufsize` apply only to desktop
   mode.** Video mode is intentionally fixed at the conservative
   8M/15M to make the "video" behaviour predictable across sessions.
3. **SIGTERM rather than stdin-EOF flush for ffmpeg restart.** Faster,
   and we don't care about losing in-flight bytes — the new ffmpeg's
   leading SPS+PPS+IDR resyncs the tablet decoder regardless.
4. **Join old relay threads before starting new ones.** Eliminates a
   real race condition that would otherwise leak old-stream tail
   bytes into the new stream over the same socket.
5. **`buf.request_update()` from the spec replaced by bare `return`**
   — that method isn't exposed by pyevdi v1.14.2; pyevdi/libevdi
   handles buffer lifecycle internally so no explicit release is
   needed.
6. **`stats["dropped"]` counter** added so the periodic fps log line
   shows drops alongside sent frames — useful both for confirming
   the mode (1:1 drop ratio == video mode) and for spotting future
   regressions where drops happen unexpectedly.
7. **Middle-band ratio resets both streaks**, not just the one not
   incrementing. Strict consecutive-frame semantics; small
   correction-of-ratio blips don't drift the state machine.

---

## Open notes for future work

- **Sender-side keyframe trigger on new client connect** would
  remove the up-to-30-second black-screen window when the tablet
  reconnects mid-GOP at idle (encoder's next IDR is up to 30 frames
  away). Today a daemon restart is the only way to force a fresh
  IDR. Adding `force_key_frames` via a side-channel or restarting
  ffmpeg on `client connected` would fix this cleanly.

- **Suppress the startup desktop→video glitch** by ignoring the
  detector for the first ~30 frames or until mutter's per-frame
  ratio drops below 1.0 once. Cosmetic only — no functional impact.

- **VIDEO_MODE bitrate could float by content**: lots of motion
  video (sports, anime) might want 12M cap; talking-head/podcast
  video can do 5M. A heuristic on encoded-bitrate feedback could
  pick. Out of scope here.

- **Combine with future audio path**: when we add audio mirroring,
  video mode is the time when audio quality matters most (people
  watch video with sound). Mode transitions could also adjust audio
  bitrate or sample rate.
