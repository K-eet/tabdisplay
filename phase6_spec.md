# Phase 6 — Clean-sheet receiver rewrite — SPEC (draft, needs approval)

## Premise

Rewrite `ReceiverActivity_v2.java` from zero, with every lesson from
Phases 1–5 baked in from line one instead of patched on afterwards.
`daemon_v2.py`, `start.sh`, and pyevdi are **frozen** at their current
verified state — the sender is the reference implementation, not part
of the rewrite. Filename, class name, package, port, and wire protocol
stay identical so nothing else in the pipeline changes.

## Non-negotiable process

1. `git init` + `.gitignore` + commit of the current tree as baseline
   **before** any rewrite work.
2. This spec committed as `phase6_spec.md`.
3. Rewrite lands as one commit; verification battery below must pass
   before it counts as done.

## Sender contract (frozen reference — receiver must assume exactly this)

* H.264 Annex B, one access unit per frame, each AU led by an AUD
  (NAL type 9, `-aud true`). 3- or 4-byte start codes possible.
* New SPS+PPS+IDR at the head of every stream; **IDR at least every
  0.5 s wall-clock** (`-force_key_frames expr:gte(t,n_forced*0.5)`).
* TCP, single client, port 7070 via `adb reverse`. On (re)connect the
  sender restarts the encoder and injects a cached frame, so the first
  bytes are always a fresh SPS+PPS+IDR — the receiver therefore
  **creates a new decoder per connection** and never tries to resync an
  existing one.
* Fixed 2560×1600. Encoder params: high profile, level 5.1, no B-frames,
  async_depth 1, VBR 20M/35M/35M @60 (desktop) / 8M/15M/15M @30 (video),
  BT.709 limited tagging.
* Cursor is kernel-blended into the framebuffer. No cursor channel
  exists; the occasional frozen ghost after fast pointer flicks is an
  accepted cosmetic limitation (heals when the cursor re-enters).

## Receiver requirements

**R1 — Connection.** TCP to localhost:7070, 5 s connect timeout,
`TCP_NODELAY`. Exactly one socket at a time.

**R2 — AU framing.** Accumulate raw bytes (64 KB reads, 4 MB cap). Split
on AUD start codes; feed one complete AU per `queueInputBuffer` with
`System.nanoTime()/1000` timestamps, flags 0. On accumulator overflow
(>4 MB without a complete AU): log, reset to empty, continue — the
0.5 s IDR cadence self-heals the resulting corruption; this dependency
is explicit, not accidental.

**R3 — Input patience.** `dequeueInputBuffer` timeout **100 ms**.
Dropping an AU under burst breaks the reference chain; waiting is
always cheaper than dropping.

**R4 — Complete draining.** Non-blocking drain after every fed AU, plus
one **5 ms blocking drain per read chunk**. The per-AU drain trails one
AU behind by construction; without the tail drain, the final frame of
any burst never renders (observed as frozen cursor/frame at idle).

**R5 — Decoder config.** AVC, 2560×1600, `COLOR_FormatSurface`,
`KEY_MAX_INPUT_SIZE` 2 MB, `KEY_LOW_LATENCY` on API ≥ 30. Created fresh
per connection, released in `finally`.

**R6 — Surface sanctity (load-bearing rule).** Nothing except MediaCodec
ever touches the Surface. No `Canvas` import, no lock/unlock, no bitmap
path anywhere in the file. (Violating this threw
`IllegalArgumentException` out of every `configure()` on One UI and cost
a full debugging session — hence a structural rule, not a comment.)

**R7 — Disconnect UX.** A plain opaque overlay View covers the
SurfaceView. Shown on the UI thread whenever a receive session ends;
hidden on the first rendered output buffer of the next session. This is
the *only* blank-screen mechanism — see R6.

**R8 — Reconnect policy.** Any failure/EOF: release decoder, show
overlay, sleep 1 s, retry. No failure counters, no backoff escalation,
no special cases.

**R9 — Stats.** Every 5 s: frames decoded, fps, Mbps in. Same line
format as today (log-parsing continuity).

**R10 — Lifecycle.** `FLAG_KEEP_SCREEN_ON`, immersive bars,
`surfaceCreated` starts the single rx thread, `surfaceDestroyed` stops
it. All codec calls on the rx thread; all view calls via
`runOnUiThread`.

## Explicit non-goals

* No cursor code of any kind. No Canvas. No audio. No resolution
  agility (log format changes, keep decoding). No reconnect backoff
  tuning. No bitrate/quality adaptation client-side.

## Structure (target ~200 lines)

`ReceiverActivity_v2` (Activity): onCreate builds
FrameLayout[SurfaceView + overlay], overlay helpers, lifecycle
callbacks. One inner `ReceiverLoop implements Runnable` with small
private methods: `openSocket`, `createDecoder`, `pump`, `feedAU`,
`drain`, `findAudStart`. Flat control flow; the only catch in the class
is the session loop's (log → overlay → sleep → retry). Error policy is
uniform: anything thrown inside `runOnce` ends the session, never the
thread.

## Verification battery (all must pass)

* **V1** — Existing daemon unit suite still green (sender untouched, but
  proves the harness works).
* **V2** — Live: single daemon, app connects first try, logcat shows
  `MediaCodec started` + steady `decoded N frames`, daemon `relay:`
  delivered ≈ encoded, zero app exceptions over 5 min idle.
* **V3** — Unplug/replug: black overlay ≤ ~1 s after yank; picture ≤
  ~2 s after replug+reconnect; daemon log shows connect → restart →
  `injected cached frame`.
* **V4** — Auto-mode round trip unchanged: fullscreen video → video
  mode with 1:1 drop ratios; pause → desktop in ~1 s; no transition at
  startup (warm-up line present).
* **V5** — Cursor: visible during normal use; fast-flick ghost may
  appear and must clear on re-entry (accepted limitation, not a failure).
* **V6** — Lifecycle: SIGTERM shuts the daemon down with no orphans;
  exactly one daemon and one ffmpeg throughout; no `Address already in
  use` in any log.

## Open questions for approval — RESOLVED

1. Keep auto desktop/video switching — **YES, keep.**
2. Keep the 3 s watchdog and sender diagnostics — **YES, keep.**
3. Non-goals stay non-goals — confirmed.
