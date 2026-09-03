# Phase 6 — Clean-sheet receiver rewrite — verification log

## What was done

Full rewrite of `ReceiverActivity_v2.java` (~340 lines) per
`phase6_spec.md` R1–R10, with git baselining done first (baseline
`4dab773`, rewrite `1a440d2`). Sender/start.sh/pyevdi frozen except two
user-requested latency items committed separately (`61ee151`:
wall-clock input timestamps, 1 MB ffmpeg pipes).

Structural improvements over the patched version (same behavior,
coherent from the start):

* `drain(decoder, info, timeoutUs)` takes the wait as a parameter — the
  per-AU drain uses 0, the burst-tail catch uses 5 ms, instead of a
  bolted-on block.
* Structurally impossible to regress the Canvas bug: no
  `android.graphics` import exists in the file (R6 as a compile-level
  rule, not a comment).
* Uniform session error policy; overflow guard documents its reliance
  on the 0.5 s IDR cadence.

## Verification battery

* **V1** — Sender unit sanity green (detector + warmup; sender
  untouched).
* **V2** — Live decode on clean rewrite: `MediaCodec started`,
  steady `decoded 5–6 frames` at idle, daemon `relay: delivered ==
  encoded`. PASS.
* **V3** — Daemon SIGTERM → app logs clean `session ended (EOF)` and
  reconnects on its 1 s loop; daemon relaunch → fresh decoder,
  `decoded 122 frames in 9.4s` ramping to `110 frames in 5.0s
  (22.0 fps, 6.0 Mbps)`. No exceptions. PASS (session path; physical
  USB yank still with user).
* **V4** — Auto-mode round trips observed on sender (user's run:
  desktop→video→desktop→video with 1:1 drop ratios, warm-up gate,
  clean Ctrl-C shutdown). Receiver doesn't participate. PASS.
* **V5** — Cursor: kernel-blended path restored (no cursor code
  anywhere in the new file; pyevdi reverted). Visual confirmation with
  user.
* **V6** — SIGTERM shutdown clean, zero orphans, single instance
  throughout. PASS.

## Latency tracing (added during this phase)

Sender `relay:` line now reports per-window encode/deliver latency
(avg/max/n + pending depth), attributed by AUD counting with
cross-chunk overlap handling (order preserved: no B-frames,
async_depth 1). Receiver stats line reports feed-gap and decode
avg/max. Both are timestamp sampling with windowed aggregates — no
per-frame logging, no hot-path allocation. First readings: encode
~87 ms single-frame at idle spin-up; decode avg ~85-95 ms at
13-22 fps (includes AU-delimit queueing by construction, not pure
decode time).

## Tooling footnotes

* Gradle builds here can serve stale dex (mixed old/new shards across
  rapid invocations; clock skew suspected). If an APK doesn't contain
  new code, `rm -rf app/build` and rebuild once — and verify with
  `grep -a <new-string> classes2.dex` (note: multidex — the code is in
  **classes2.dex**, not classes.dex).
* `adb kill-server/start-server` revived a detached tablet session.
* /tmp scratch does not survive across sessions — keep test scripts in
  the repo if they need to live longer than a night.
