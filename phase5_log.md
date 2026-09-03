# Phase 5 — Hardware cursor channel — implementation log

> **STATUS: REVERTED (same night).** The channel worked end-to-end but
> the composited cursor rendered as a black box on the tablet (root cause
> not identified before the user called it — sprite data verified good,
> blend math verified against the kernel's own, so the suspicion falls on
> pixel-format/stride semantics of the live event path vs. the captured
> dump). User accepted the original kernel-blend behaviour — including
> its cosmetic frozen-ghost — as not a dealbreaker. All Phase 5 code was
> surgically removed from `daemon_v2.py` and pyevdi; the module was
> rebuilt without cursor bindings and `evdi_enable_cursor_events` is no
> longer called, restoring kernel-side blending. This file is kept as an
> investigation record; the findings below remain valid and would be the
> starting point for a future attempt (prime suspect for next time:
> verify the event sprite's alpha byte position against the actual
> DRM_FORMAT_* on the live path, not just the captured dump — and test
> blend output against a known background frame-by-frame, not by eye).

Goal: eliminate the frozen-cursor ghost reported on the Tab S7 FE and
restore native-feeling cursor behaviour over the evdi link.

Root cause chain (established empirically, see "Investigation" below):

* evdi's kernel painter **blends the cursor sprite into every grabbed
  framebuffer** while `cursor_events_enabled` is false
  (`evdi_painter.c:1117`). The blended state is the kernel's own
  `evdi->cursor`, which goes stale: when the pointer leaves the output,
  mutter stops committing plane updates, nothing erases the last baked
  sprite, and the ghost persists until the pointer re-enters and damages
  that region. This was the ghost — present since Phase 1, only noticed
  once Phase 4's latency work made frames trustworthy enough to see it.
* Setting `evdi_enable_cursor_events(handle, true)` flips the kernel into
  "userspace owns the cursor" mode: blending stops, and cursor_set /
  cursor_move events flow to the client. The client must then composite
  the sprite itself — exactly like a real DisplayLink host.

Status: **DONE.** Ghost eliminated structurally; cursor composited in
userspace from live events; movement synthesised at pointer rate.

---

## What changed

### 1. pyevdi: cursor event bindings (`Card.h/.cpp`, `PyEvdi.cpp`)

* `Card::connect()` now calls `evdi_enable_cursor_events(handle, true)`.
* New C handlers forward `evdi_cursor_set` / `evdi_cursor_move` to Python:
  * `cursor_set_handler(dict)` — hot_x, hot_y, width, height, enabled,
    pixel_format, stride, buffer (**bytes copy** — libevdi mmaps and
    mallocs a private copy per event, but the pointer is only valid for
    the callback duration).
  * `cursor_move_handler(dict)` — x, y.
* `py::gil_scoped_acquire` in the C trampolines; handlers are no-ops when
  the Python attribute is None.
* Rebuilt `PyEvdi.cpython-312-x86_64-linux-gnu.so` in place.

### 2. `daemon_v2.py`: userspace cursor compositor

* `cursor_state` — enabled / sprite (h×w×4 uint8) / size / position.
* `blend_cursor(payload)` — composites the sprite into a **copy** of the
  outgoing BGRX frame (never mutates evdi's buffer). Semantics mirror the
  kernel's `evdi_cursor_compose_and_copy()`: sprite centred on the
  reported position, per-channel rounded fixed-point lerp
  `((bg*(255-a) + fg*a) + ((v+1)<<8)) >> 16`. Zero-copy fast path when
  the cursor is hidden, sprite-less, CLI-disabled, or fully off-screen.
* **Cursor-movement synthesis**: in plane-cursor mode mutter does NOT
  damage the framebuffer on pointer movement, so without synthesis the
  composited cursor would only refresh at idle-damage rate (~1 fps).
  `maybe_synthesize()` re-blends the cached clean framebuffer
  (`state["last_base"]`, maintained only while the cursor is near this
  output) and writes it to ffmpeg at up to 60 fps on every cursor_move /
  cursor_set. Desktop mode only — video mode's frame dropper owns the
  encoder cadence. Writes go through `restart_lock` like all stdin I/O.
* `--no-cursor-composite` escape hatch.
* Diagnostics: throttled cursor_set/move/blend logging, one-off sprite
  dump to `/tmp/cursor_sprite.ppm`, `SIGUSR1` dumps the current
  post-blend framebuffer to `/tmp/tabdisplay_dump.ppm` (bgr0 builds).

### 3. Bugs found and fixed along the way

* **Broadcast shape error** in the first blend (`region` 4 channels ×
  `fg` 3 channels): raised on every frame with the cursor on-screen; the
  callback's exception guard swallowed it and dropped those frames —
  manifesting as "cursor invisible". Found by unit-testing the blend
  offline; 38 occurrences confirmed in the log afterwards.
* **Process-hygiene incident**: a stale pidfile let two daemons run
  concurrently, fighting over TCP 7070 and two evdi cards (black screen,
  interleaved streams). Cleaned up; always `pgrep -af` before restarting.
* Two "black screen" reports during debugging were **not** bugs: the
  laptop auto-locks after 5 idle minutes (mirrored faithfully), and the
  extended desktop is empty except the top bar when no windows are placed
  on it.

---

## Investigation notes (for future archaeology)

* Cursor event payloads observed on GNOME 46 / mutter atomic:
  64×64, `DRM_FORMAT_ARGB8888` ("AR24"), stride 256, hot (0,0),
  buffer_length 16384, enabled toggles at output boundaries. Coordinates
  are **framebuffer-local** (boundary crossings at x≈0 confirm it; the
  global-logical theory was tested and rejected).
* Sprite content: GNOME theme cursor — black fill, thin white outline
  (39 white / 3669 black px in the captured resize-arrows). The "black
  blob" report was this genuine sprite against dark terminal content.
* libevdi mmaps the cursor GEM object per event and copies it — no
  lifetime tricks needed beyond per-callback copying.
* Premultiplied-alpha ambiguity remains open; the current straight-alpha
  lerp matches the kernel's own blender, so parity is guaranteed either
  way.

## Latency

Synthesised frames cap at 60 fps (16 ms) + async_depth-1 encoder + pipe +
MediaCodec ≈ 100-150 ms glass-to-glass for cursor motion. User-reported
as "smooth but has latency" — accepted for now; further reduction would
need encoder-bypass experiments (out of scope).

## Open notes

- If a future attempt resumes this work: the pyevdi binding code in this
  log's git-less history (or re-derive from `evdi_lib.h` — it's ~80
  lines) plus the daemon blend/synthesis design below are proven except
  for the final pixel correctness. Suggested first step: feed a
  synthetic 8x8 ARGB sprite through the exact event path and dump the
  blended output before touching the live pipeline.
- `KEY_LOW_LATENCY` + tail-drain receiver changes from Phase 4 remain in
  place.
- The kernel-side ghost mechanism still exists for the reverted
  configuration — accepted trade-off.
