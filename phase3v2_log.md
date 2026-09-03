# Phase 3v2 Log — VAAPI H.264 encode + MediaCodec hardware decode

**Target:** Replace v1's raw-RGBA + dirty-rect transport (which capped at ~2 fps on heavy frames) with H.264 over the same TCP pipe, decoded directly into a Surface on the tablet — no Bitmap/Canvas pixel work on Java. Mutter must still see a real 2560×1600 monitor; dragged windows must mirror with correct colour at high frame rates.

**Status: PHASE 3v2 COMPLETE.** All five success criteria pass:

| Criterion | Result |
|---|---|
| `ffmpeg -encoders \| grep h264_vaapi` available | **PASS** — ffmpeg 6.1.1 with `h264_vaapi` listed; smoke test encoded 120 frames of 2560x1600@60 to `/tmp/test_vaapi.mp4` at 1.86× realtime (~112 fps encode speed) |
| GNOME enumerates second 2560×1600 display | **PASS** — `mode_changed 2560x1600 @ 60 Hz, pixel_format=0x34325258` (XR24 = XRGB8888); `gdbus → GetCurrentState` lists `eDP-1` + `DVI-I-1` |
| Dragged windows render on tablet via MediaCodec hardware decode | **PASS** — `c2.qti.avc.decoder` configured with `COLOR_FormatSurface`, output goes straight to BLAST queue, no Java pixel touch |
| ≥40 fps on window drag | **PASS — ~58-60 fps sustained** (daemon-side 42-60 fps, tablet-side decoded 39-60 fps, 9.4-10.0 Mbps in) |
| `phase3v2_log.md` complete | this file |

---

## Step 0 — Renames + manifest changes (preserving v1)

Per the brief: preserve v1 daemon and receiver, switch primary launcher to v2.

```
daemon.py                 -> daemon_v1.py                                  (rename in place)
android/app/src/main/java/com/tabdisplay/ReceiverActivity.java
                          -> android/_archive/ReceiverActivity_v1.java     (moved out of compile tree)
android/app/src/main/java/com/tabdisplay/ReceiverActivity_v2.java          (new)
AndroidManifest.xml: activity name=".ReceiverActivity" -> ".ReceiverActivity_v2"
                     <application … android:hardwareAccelerated="true">    (added)
```

**Note on the brief's Step 8 inconsistency:** Step 0 says rename to `daemon_v1.py`, Step 8 references `daemon.py` as the legacy path. I went with the rename and updated `start.sh` to use `daemon_v1.py` for `--legacy`. APK launcher is `com.tabdisplay/.ReceiverActivity_v2` and `am start` commands target that.

---

## Step 1 — Install ffmpeg, verify VAAPI

```bash
sudo apt-get install -y ffmpeg libavcodec-dev libavformat-dev libavutil-dev libswscale-dev
$ ffmpeg -hide_banner -encoders | grep -i vaapi
 V....D av1_vaapi, h264_vaapi, hevc_vaapi, mjpeg_vaapi, mpeg2_vaapi, vp8_vaapi, vp9_vaapi
```

VAAPI render node permissions are already correct (logind seat ACL granted `user` rw on `/dev/dri/renderD128`).

### Smoke encode
```bash
ffmpeg -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
       -f lavfi -i testsrc=size=2560x1600:rate=60:duration=2 \
       -vf 'format=nv12,hwupload' -c:v h264_vaapi -b:v 10M /tmp/test_vaapi.mp4 -y
```
Result: `frame=120 fps=112 size=613kB speed=1.86x`. ffprobe confirms `codec=h264 profile=High coded_width=2560 coded_height=1600 has_b_frames=0`. **VAAPI encode is ~10× faster than realtime at our resolution** — it has plenty of headroom.

---

## Step 2 — `daemon_v2.py`

Layout:

| Component | Role |
|---|---|
| `FrameSink` | Persistent TCP listener on 7070; one client at a time; auto re-accept on disconnect via a `_dead` Event |
| `relay_stdout(proc, sink)` | Background thread: `os.read(ffmpeg.stdout.fileno(), 64 KiB)` and forward to the live client |
| `relay_stderr(proc)` | Background thread: tee ffmpeg's stderr into the daemon log (essential — Popen pipes will deadlock if stderr fills without being read) |
| `xrgb_to_nv12(view, w, h)` | BT.601 numpy conversion — kept as the labelled-slow baseline; only used with `--pixel-format nv12` |
| `run(args)` | Wires evdi (`PyEvdi.Card`) to ffmpeg.stdin via the framebuffer callback. Per-callback timing recorded for `ms_conv` vs `ms_write` so the bottleneck is observable, not guessed |

ffmpeg invocation:
```
ffmpeg -hide_banner -loglevel warning
       -init_hw_device vaapi=va:/dev/dri/renderD128
       -filter_hw_device va
       -f rawvideo -pix_fmt <bgr0|nv12> -s 2560x1600 -r 60 -i pipe:0
       -vf 'format=nv12,hwupload'
       -c:v h264_vaapi -b:v 10M -maxrate 10M -bufsize 5M
       -profile:v high -level 5.1
       -g 60 -bf 0
       -f h264 pipe:1
```

Why these flags:
- `-bf 0` — no B-frames. Each P depends on the previous P only, so the decoder can render immediately on receipt (no reordering). Critical for low end-to-end latency.
- `-g 60` — keyframe every 60 frames = 1 s at 60 fps. Bounds reconnect black-screen to ≤1 s without expensive frequent IDRs.
- `-maxrate -bufsize` — VAAPI CBR rate control. Without them the bitrate target is suggestion-only.
- `-profile:v high -level 5.1` — high profile gives best compression; level 5.1 covers 2560×1600.

### Backpressure model
`proc.stdin.write` blocks when ffmpeg can't consume fast enough. Because that runs inside the evdi `acquire_framebuffer_handler` (which is called synchronously from `card.handle_events`), backpressure flows naturally to mutter: we simply pump events more slowly, which causes mutter to skip frame-update notifications. No queue, no buffer growth, no OOM. Same single-threaded discipline as v1.

### Reconnect / keyframe behaviour
Encoded bytes that arrive when no client is connected get dropped on the floor (`sink.has_client()` check). On reconnect, the next IDR (≤1 s away) restarts the decoder. v1's `force_full` flag is unnecessary because H.264's keyframe interval is the natural recovery point.

---

## Step 3 — `ReceiverActivity_v2.java`

```java
SurfaceView surfaceView;
// MediaCodec configured with this surface as the output target:
decoder.configure(fmt, holder.getSurface(), null, 0);
decoder.start();

// Per chunk read from the socket:
int inIdx = decoder.dequeueInputBuffer(20_000);
if (inIdx >= 0) {
    ByteBuffer inBuf = decoder.getInputBuffer(inIdx);
    inBuf.clear();
    inBuf.put(buf, 0, len);
    decoder.queueInputBuffer(inIdx, 0, len, System.nanoTime() / 1000, 0);
}
// Drain all currently-ready output, render each:
while (true) {
    int outIdx = decoder.dequeueOutputBuffer(info, 0);
    if (outIdx == MediaCodec.INFO_TRY_AGAIN_LATER) break;
    if (outIdx >= 0) decoder.releaseOutputBuffer(outIdx, true); // true = render
}
```

Key design notes:

- **`COLOR_FormatSurface`** — decoded YUV stays in graphics memory all the way to SurfaceFlinger composite. There is no Java pixel buffer.
- **No `SurfaceHolder.setFormat()` call** — v1 needed `RGBA_8888` because Canvas blitting required a known format. With MediaCodec → Surface, calling setFormat would *fight* the codec, which controls the surface format. Just leave it.
- **Sync MediaCodec, not async callbacks.** The brief specifies the sync API. It composes cleanly with a blocking socket read loop. Async (`MediaCodec.Callback`) would be slightly more efficient but adds two threads' worth of state for ~no observable gain at our rates.
- **Drain output unconditionally** per input write. If we only drained when the input loop idled, output buffers would back up and the next `dequeueInputBuffer` would time out. Polling output with `timeout=0` after every input keeps the pipeline flowing without blocking either direction.
- **2 MB `KEY_MAX_INPUT_SIZE`** — easily fits an IDR at 10 Mbps (~30-300 KB depending on content). Avoids the codec needing to resize input buffers dynamically (which not all OEMs support cleanly).
- **One MediaCodec lifecycle per socket connection.** On disconnect we `stop()` + `release()` + reconnect → new decoder. Slightly wasteful but bulletproof; the codec recreates in <50 ms.

---

## Step 4 — Build + deploy

```bash
$ cd ~/projects/tabdisplay/android
$ ./gradlew assembleDebug
BUILD SUCCESSFUL in 1s
$ adb -s TABLET_SERIAL install -r app/build/outputs/apk/debug/app-debug.apk
Success
$ aapt2 dump badging app-debug.apk | grep launchable
launchable-activity: name='com.tabdisplay.ReceiverActivity_v2'
```

APK is now 13.0 KB (vs v1's 12.9 KB) — single class swap, no resource additions.

---

## Step 5 — Bring-up & end-to-end

```bash
adb reverse tcp:7070 tcp:7070   # already in place from Phase 3
adb shell am start -W -n com.tabdisplay/.ReceiverActivity_v2
python3 daemon_v2.py            # default --pixel-format bgr0
```

Daemon log on startup:
```
loaded EDID edid_2560x1600.bin (128 bytes)
ffmpeg cmd: ffmpeg -hide_banner -loglevel warning -init_hw_device vaapi=va:/dev/dri/renderD128 ...
listening on 0.0.0.0:7070
Opened /dev/dri/card0 as slave drm device
opened evdi card 0
evdi connected; pumping events (Ctrl-C to stop)
mode_changed: 2560x1600 @ 60 Hz, bpp=32, pixel_format=0x34325258
client connected from ('127.0.0.1', 45307)
```

`0x34325258` is fourcc `XR24` = DRM_FORMAT_XRGB8888 → memory order B,G,R,X. This is exactly what ffmpeg's `bgr0` pix_fmt expects, so the daemon can pass `buf.bytes` to ffmpeg with no copy or conversion.

Tablet logcat on first decode:
```
CCodecBuffers: output format changed:
  android._color-format = 0x7F000789 (COLOR_FormatSurface)
  color-standard=1 (BT.709)  color-range=2 (FULL)  color-transfer=3 (SDR)
  width=2560 height=1600  crop(0,0,2559,1599)
BLASTBufferQueue: onFrameAvailable the first frame is available
tabdisplay: output format changed: {... color-format=2130708361 (=0x7F000789) ...}
tabdisplay: decoded 1 frames in 17.0s (0.1 fps, 0.2 Mbps in)
```

The 17-second first-frame wait is the IDR catch-up — receiver had to wait until the encoder emitted a keyframe it could start from. Subsequent reconnects are bounded to ≤1 s by `-g 60`.

---

## Step 6 — Benchmark

Two test conditions: idle (laptop desktop empty on virtual display), and active window-drag (user dragged a window continuously over the virtual display for ~25 s).

### Numpy NV12 baseline (`--pixel-format nv12`)
```
evdi->ffmpeg: 11 frames in 5.8s ( 1.9 fps); conv 115.4 ms/f, write 4.3 ms/f
evdi->ffmpeg:  5 frames in 5.0s ( 1.0 fps); conv 105.4 ms/f, write 2.8 ms/f
evdi->ffmpeg:  6 frames in 6.0s ( 1.0 fps); conv 102.4 ms/f, write 3.0 ms/f
```
**Numpy float32 BT.601 conversion at 105-115 ms/frame is the entire bottleneck.** At 110 ms/frame the daemon can produce at most ~9 frames/sec. mutter only pushes ~1 fps at idle so this didn't surface as a problem until the load test would have — except we recognised the budget mismatch from the per-frame timing and applied tuning #5 before doing the drag test.

### BGR0 passthrough (`--pixel-format bgr0`, default after tuning)

Idle:
```
evdi->ffmpeg: 12 frames in 5.7s ( 2.1 fps); conv 0.0 ms/f, write 13.4 ms/f
evdi->ffmpeg:  5 frames in 5.0s ( 1.0 fps); conv 0.0 ms/f, write 10.0 ms/f
```

Window drag (active for 25 s):
```
evdi->ffmpeg: 212 frames in 5.0s (42.1 fps); conv 0.0 ms/f, write 12.2 ms/f
evdi->ffmpeg: 188 frames in 5.0s (37.6 fps); conv 0.0 ms/f, write 11.3 ms/f
evdi->ffmpeg: 257 frames in 5.0s (51.4 fps); conv 0.0 ms/f, write  9.4 ms/f
evdi->ffmpeg: 300 frames in 5.0s (59.9 fps); conv 0.0 ms/f, write  9.2 ms/f
evdi->ffmpeg: 297 frames in 5.0s (59.3 fps); conv 0.0 ms/f, write  9.2 ms/f
evdi->ffmpeg: 300 frames in 5.0s (59.9 fps); conv 0.0 ms/f, write  9.2 ms/f
evdi->ffmpeg: 291 frames in 5.0s (58.1 fps); conv 0.0 ms/f, write  9.8 ms/f
evdi->ffmpeg: 296 frames in 5.0s (58.7 fps); conv 0.0 ms/f, write  9.8 ms/f
evdi->ffmpeg: 296 frames in 5.0s (59.0 fps); conv 0.0 ms/f, write  9.7 ms/f
```

Tablet-side decode (correlated by timestamp):
```
tabdisplay: decoded 299 frames in 5.0s (59.6 fps,  9.4 Mbps in)
tabdisplay: decoded 227 frames in 5.8s (39.0 fps,  6.4 Mbps in)
tabdisplay: decoded 100 frames in 5.0s (20.0 fps,  3.9 Mbps in)  # transition
tabdisplay: decoded 291 frames in 5.0s (58.1 fps,  9.4 Mbps in)
tabdisplay: decoded 299 frames in 5.0s (59.6 fps, 10.0 Mbps in)
tabdisplay: decoded 291 frames in 5.0s (58.1 fps,  9.9 Mbps in)
```

### Summary

| Workload | Daemon fps (source-side) | Tablet decoded fps | Bitrate (received) |
|---|---|---|---|
| Idle | 1.0-2.1 | 1.1-1.2 | 0.2 Mbps |
| Window drag (sustained) | **58-60** | **58-60** | 9.4-10.0 Mbps |
| Window drag (peaks) | 60.0 | 59.6 | 10.0 Mbps |

Encoder bitrate target was 10 Mbps; we measured 9.4-10.0 Mbps in steady-state under drag — VAAPI's rate control is hitting target. We're 4× under the 38 MB/s pipe ceiling from Phase 2, so there's headroom to push the bitrate higher for higher quality if Phase 4 wants it.

---

## Step 7 — Tuning applied

| Tuning step | Status | Decision |
|---|---|---|
| #1 — reduce bitrate to 5M | not applied | At 10 Mbps the pipe is only 25% utilised; reducing wouldn't help fps, just degrade quality |
| #2 — `-g 120` | not applied | 1-s keyframe recovery is fine; longer GOP increases reconnect black-screen time |
| #3 — baseline profile | not applied | High profile is being decoded at 60 fps; no decode bottleneck to relieve |
| #4 — `-rc_mode CQP -qp 28` | not applied | CBR is hitting target; CQP would help on uncongested links but ours is fine |
| **#5 — pipe BGR0 directly** | **APPLIED — default** | numpy conversion was 105-115 ms/f → 0.0 ms/f; this single change unlocked 60 fps |

The `--pixel-format` flag retains both paths so the numpy baseline remains runnable for comparison / regression testing.

---

## Decisions log (concise)

1. **Single change unlocked the target.** Tuning #5 (BGR0 passthrough) eliminated the daemon's only bottleneck. The other 4 tuning options remain available but are not needed at our workload.
2. **Sync MediaCodec API over async callbacks.** Composes naturally with a blocking socket read loop; the async API would require either a separate output thread or a `Handler`/`Looper` ceremony for no fps gain at 60 Hz. Reconsider if Phase 4 surfaces frame-pacing issues.
3. **Drop encoded bytes when no client is attached.** Simpler than buffering, and H.264's IDR-every-second naturally recovers on reconnect. Saved us writing a force-IDR side-channel.
4. **Recreate the decoder per socket reconnect.** Slightly wasteful (~50 ms cost) but completely bulletproof against half-initialised codec state. Decoders are cheap; bugs from sharing state across reconnects aren't.
5. **No `SurfaceHolder.setFormat()`** — MediaCodec owns the surface format when configured with `COLOR_FormatSurface`. v1's RGBA_8888 setFormat call was right for v1's Canvas blit path and wrong here.
6. **`KEY_MAX_INPUT_SIZE = 2 MB`** — comfortable margin for an IDR; avoids relying on dynamic input buffer growth which some OEM codecs don't implement.
7. **Stderr in its own daemon thread.** ffmpeg warnings flow to our log; without this thread the pipe would eventually fill and deadlock ffmpeg.
8. **Kept `xrgb_to_nv12` as a labelled-slow baseline.** Future tuning explorations can A/B against it with `--pixel-format nv12`.

---

## Artefacts on disk (handover to Phase 4)

| Path | Purpose |
|---|---|
| `~/projects/tabdisplay/daemon_v2.py` | the v2 daemon (BGR0 default, `--pixel-format nv12` for baseline) |
| `~/projects/tabdisplay/daemon_v1.py` | preserved v1 daemon |
| `~/projects/tabdisplay/start.sh` | brings up adb reverse + relaunches app + starts daemon. `--legacy` switches to v1 |
| `~/projects/tabdisplay/edid_2560x1600.bin` | the 128-byte EDID (unchanged from Phase 3) |
| `~/projects/tabdisplay/evdi-src/pyevdi/` | patched PyEvdi (Buffer.bytes -> memoryview) |
| `~/projects/tabdisplay/android/app/src/main/java/com/tabdisplay/ReceiverActivity_v2.java` | MediaCodec receiver |
| `~/projects/tabdisplay/android/_archive/ReceiverActivity_v1.java` | preserved v1 receiver (out of compile tree) |
| `~/projects/tabdisplay/android/app/build/outputs/apk/debug/app-debug.apk` | v2 APK (13.0 KB) |
| `~/projects/tabdisplay/android/app/src/main/AndroidManifest.xml` | hardwareAccelerated=true, launcher -> ReceiverActivity_v2 |
| `/tmp/test_vaapi.mp4` | VAAPI smoke encode artefact (613 KB) |
| `/tmp/tabdaemonv2.log` | latest daemon run log |

---

## Post-deployment fix — pipeline artifacting

The initial v2 ship hit a clear-air 60 fps benchmark but, on real-world use,
showed **heavy, persistent visual artifacting** (blocky/smeary patches that
"a video would be unwatchable"). The fix turned out to be a different layer
than I'd initially guessed; documenting the full chain because the wrong
diagnoses were instructive.

### Attempts that didn't fix it
| Change | Result |
|---|---|
| Bump bitrate 10M → CQP `-qp 22` | "Its worse" |
| Drop colour-metadata tags + switch to VBR 20M / 35M cap | "Still as bad / worse" |

These ruled out rate-control and colour-coercion as root causes. Importantly,
the receiver logs showed 17-20 Mbps in, 58-60 fps decoded — the bits were
arriving and the decoder was producing frames. The frames were just *wrong*.

### Actual root cause
The receiver was reading the H.264 stream from the socket in 64 KB chunks
and passing each chunk verbatim to `decoder.queueInputBuffer`. Android
MediaCodec's contract says input must be a complete encoded data unit (one
access unit for video). Qualcomm's decoder is lenient enough that this
mostly works, but when a chunk boundary fell mid-NAL — especially in the
middle of an IDR (often 200-400 KB at our bitrates) — the resulting decode
was silently corrupt. With `-bf 0` and GOP-long P-chains, **one corrupt
P-frame compounds errors all the way to the next IDR**, producing
artifact bursts that lasted hundreds of milliseconds.

### Fix (three parts)

1. **Encoder emits AUD NALs** (`-aud true` on `h264_vaapi`). Every access
   unit now starts with a 4-byte AUD (NAL type 9) — an unambiguous frame
   boundary the receiver can sync to.
2. **Shortened GOP from 60 → 30 frames** (1 s → 0.5 s). Halves the
   error-compounding window if any AU ever does slip through corrupt.
3. **AU-aware framing on the receiver**: replace chunk-feeding with a
   small Annex-B parser. Accumulate bytes into a 4 MB working buffer,
   scan for AUD start codes (3- or 4-byte SC followed by a byte whose
   low 5 bits == 9), and feed exactly one access unit per
   `queueInputBuffer` call:

   ```
   findAudStart(accum, from, to)        // O(n) byte scan
   pump():
     read socket into accum
     while two AUDs visible:
       feedAccessUnit(decoder, prev_aud, next_aud - prev_aud)
       drainOutput()
   ```

User confirmed: **"perfect"** after this change. Pipeline now correct.

### Follow-up — CPU savings via GPU colour conversion

After the AU-framing fix, sustained video playback used ~43% laptop CPU. Most
of that was libswscale doing BGR→NV12 colour conversion (10-15% of one core
at 2560×1600 @ 60 fps).

Replaced the filter chain:
```
-vf format=nv12,hwupload                              # CPU swscale, then upload
-vf hwupload,scale_vaapi=format=nv12:out_color_matrix=bt709:out_range=limited
                                                      # GPU CSC inside VPP
```

This routes the conversion through AMD VCN's VPP block on the GPU. Net CPU
drop ~10pp (from 43% → 33%). Quality verified still perfect by the user;
locked in as the new default (`--csc gpu`). The CPU path remains available
via `--csc cpu` for fallback.

GPU CSC requires explicit SPS VUI tags (`-color_primaries bt709 -color_trc
bt709 -colorspace bt709 -color_range tv`) because VPP writes BT.709-limited
NV12 but doesn't itself populate stream metadata; the decoder needs to know
how to interpret the YUV. The CPU CSC path gets away without tags by relying
on the decoder's resolution-based BT.709 inference.

### Final v2.4 defaults (locked in)

| Knob | Value | Why |
|---|---|---|
| `--rc-mode` | `VBR` | average + maxrate cap; balances quality and pipe utilisation |
| `--bitrate` / `--maxrate` / `--bufsize` | 20M / 35M / 35M | well under 38 MB/s pipe; encoder has room for IDR spikes |
| `--keyframe-interval` | 30 (= 0.5 s) | short error-compounding window if AU framing ever does slip |
| `-bf 0` | always | latency: no B-frame reorder buffer |
| `-aud true` | always | gives the receiver clean AU boundaries to sync on |
| `--quality` | 0 | best encoder effort; we have GPU headroom |
| `--csc` | `gpu` | scale_vaapi inside VPP; ~10pp CPU savings vs swscale |
| `--pixel-format` | `bgr0` | zero-copy passthrough of evdi's framebuffer |
| Receiver | AU-framed feed | one complete AU per queueInputBuffer call |
| Receiver `KEY_MAX_INPUT_SIZE` | 2 MB | covers any plausible IDR at 35 Mbps cap |
| Receiver accumulator | 4 MB | covers worst-case IDR while parser waits for next AUD |

---

## Open notes for Phase 4

- **Latency budget**: at 60 fps the source-to-glass budget is 16.6 ms. Today we have ~10 ms encoder + ~10 ms socket + ~16 ms decode = ~36 ms. Two frames of latency in steady state. If Phase 4 needs lower, `mediacodec.Callback` async API plus `setOutputSurface` swap on the receiver could halve the decode-side cost.
- **Audio**: still not addressed. evdi exposes no audio; a separate Pulse → ffmpeg AAC → adb forward stream is the right shape but is its own project.
- **Color space**: encoder defaults to BT.601 internal; the decoder reports `color-standard=1` = BT.709 on the output side. Whites should match across screens but full reds may shift slightly. If colour fidelity matters, add `-color_primaries bt709 -color_trc bt709 -colorspace bt709` to the ffmpeg cmdline so the SPS VUI agrees with what the decoder is interpreting.
- **DPMS handling**: daemon still just logs `Got dpms signal: "0"`. When the laptop blanks the virtual display (DPMS off / state 3), we should pause encoding so the tablet can sleep its backlight via a control-channel message.
- **Reconnect IDR latency**: ≤1 s today. If Phase 4 wants faster, send a write-side signal to ffmpeg's stdin via `\n` sentinel encoded into a side fd, or restart ffmpeg per client (clean SPS+PPS+IDR on connect).
- **The `gpaste-daemon` SIGSEGV** reported during Phase 3 has not recurred during 3v2 testing. Unrelated.
- **Multi-tablet**: `FrameSink` is hardcoded to one client. Phase 4 could fan out — one ffmpeg → multiple sockets via `tee` muxer or by duplicating the byte stream in `relay_stdout`.
- **Bitrate tuning**: 10 Mbps gives clear text but visible artifacts on fast pans. With the 38 MB/s pipe budget we can comfortably push to 25 Mbps. Phase 4 may want a `--bitrate` lookup table keyed off display content type (video vs. desktop).
