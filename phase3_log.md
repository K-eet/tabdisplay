# Phase 3 Log — pyevdi daemon, dirty-rect protocol, multi-monitor mirror

**Target:** Bridge mutter's framebuffer for our evdi virtual display to the Android receiver. mutter must see a real second 2560×1600 monitor; windows dragged onto it must render on the tablet, with correct colours.

**Status: PHASE 3 COMPLETE.** All four success criteria from the brief are met:

| Criterion | Result |
|---|---|
| GNOME shows a second display at 2560×1600 | **PASS** — `mode_changed: 2560x1600 @ 60 Hz, bpp=32, pixel_format=0x34325258 (DRM_FORMAT_XRGB8888)`, GNOME `gdbus` lists `eDP-1` + `DVI-I-1`, sysfs connector status `connected` |
| Dragging windows onto the virtual display mirrors them on the tablet | **PASS** — confirmed visually |
| Colour test pattern shows correct R/G/B (byte swap verified) | **PASS** — tablet ended on magenta after a green/blue/white/magenta cycle that was synthesised in XRGB8888 and routed through `xrgb_to_rgba` |
| `phase3_log.md` complete | this file |

One observed but accepted limitation: **lag during heavy window motion**. Idle is 1 fps / 20 KB/s — basically free. Active drags spike to ~30 MB/s and 2-8 fps because each full-frame fallback is 16.4 MB raw RGBA over a ~38 MB/s adb-reverse pipe. Phase 4's compression brief is the right fix.

---

## Step 0 — Two upstream changes that fall out of Phase 3

These weren't called out in the brief but are necessary to make Phase 3 work:

1. **PyEvdi binding patch — `Buffer.bytes` was unusable from Python.**
   `pyevdi/PyEvdi.cpp` v1.14.2 binds `Buffer.bytes` as the raw `void*` of the underlying buffer. pybind11 casts `void*` to an opaque Python capsule — you can't `np.frombuffer(...)` it or read pixels through it. Patched the binding to return a real zero-copy `py::memoryview::from_memory(self.buffer.buffer, self.buffer_size)` and additionally exposed `buffer_size` as a property:

   ```cpp
   .def_property_readonly("bytes",
       [](Buffer &self) {
           return py::memoryview::from_memory(self.buffer.buffer,
                                              self.buffer_size);
       })
   .def_property_readonly("buffer_size",
       [](Buffer &self) { return self.buffer_size; })
   ```

   Rebuild: `cd evdi-src/pyevdi && make clean && make` → fresh `PyEvdi.cpython-312-x86_64-linux-gnu.so`. Confirmed `dir(PyEvdi.Buffer)` now includes `buffer_size` and reading `buf.bytes` yields a memoryview of `width*height*4` bytes.

2. **SurfaceView format change in `ReceiverActivity.java`.**
   Phase 2's logcat showed SurfaceFlinger compositing our SurfaceView as `RGB_565`, dithering down our ARGB_8888 backing bitmap. Added `surfaceView.getHolder().setFormat(PixelFormat.RGBA_8888)` in `onCreate` — Phase 3's logcat now reads `RGBA_8888` for the BLAST surface. Real colour fidelity restored.

---

## Step 1 — EDID generation

Generator: `~/projects/tabdisplay/generate_edid.py`. Output: `~/projects/tabdisplay/edid_2560x1600.bin` (128 bytes).

EDID 1.3 base block, hand-assembled. Key values:
- Manufacturer: `QCM` → 0x446D (BE, bytes 8-9)
- Product code: 0x0001
- Made in: week 1 of 2026
- Digital input; sRGB primary; preferred DTD is native
- Physical image size: 26 cm × 17 cm (263 × 165 mm in the DTD)
- Gamma: 2.20
- Preferred DTD (CVT-RB 2560×1600 @ 60 Hz):
  - pclk 268.500 MHz, htotal 2720, vtotal 1646
  - Hfront 48 / Hsync 32 / Hback 80, +Hsync
  - Vfront 3 / Vsync 10 / Vback 33, +Vsync
- Monitor range limits: 50-75 Hz V, 30-110 kHz H, max 270 MHz
- Monitor name: `TabDisplay`
- One dummy descriptor (tag 0x10) to fill the fourth descriptor slot.

### `edid-decode` verification (excerpt)
```
EDID Structure Version & Revision: 1.3
Manufacturer: QCM   Model: 1   Made in: week 1 of 2026
Digital display
Maximum image size: 26 cm x 17 cm
Gamma: 2.20
RGB color display
Default (sRGB) color space is primary color space
First detailed timing is the preferred timing
DTD 1:  2560x1600   59.971589 Hz   8:5     98.713 kHz   268.500000 MHz (263 mm x 165 mm)
             Hfront   48 Hsync  32 Hback   80 Hpol P
             Vfront    3 Vsync  10 Vback   33 Vpol P
Display Range Limits:
  Monitor ranges (GTF): 50-75 Hz V, 30-110 kHz H, max dotclock 270 MHz
Display Product Name: 'TabDisplay'
Dummy Descriptor:
Checksum: 0x92
```
Zero errors from edid-decode.

---

## Step 2/3 — `daemon.py`

Path: `~/projects/tabdisplay/daemon.py`. Structure:

| Component | Responsibility |
|---|---|
| `FrameSink` | Persistent TCP listener on 7070; holds at most one client; background `acceptor` thread, with `_dead` Event signalled from the send path when a write raises. Auto re-accepts. |
| `xrgb_to_rgba(view, w, h)` | Byte swap. Builds an HxWx4 RGBA uint8 via column reorder: `out[..,0] = src[..,2]; out[..,2] = src[..,0]; out[..,3] = 255`. Source is the memoryview from `Buffer.bytes`. |
| `normalize_rects(raw_rects, w, h, threshold=0.8)` | Clip each `evdi_rect` to the frame, drop empties; if total dirty area > 80% of screen (or zero rects), collapse to one full-frame rect. |
| `encode_packet(rgba, rects)` | Pack `[u32 numRects][per-rect: u32 x1, u32 y1, u32 x2, u32 y2, u32 len][N RGBA bytes]` as a single `b"".join(...)`. |
| `run_evdi(sink, edid_path)` | Find AVAILABLE card via `pick_evdi_card`, open `PyEvdi.Card`, wire `mode_changed_handler` + `acquire_framebuffer_handler`, call `card.connect(edid, len(edid), sku_area, sku_pclk)`, then loop `card.handle_events(500)`. |
| `run_test_pattern(sink, period)` | Bypasses evdi; synthesises XRGB8888 source buffers and routes them through `xrgb_to_rgba` so the swap path *is* exercised by the test (initial draft synthesised RGBA directly, which couldn't catch a swap bug — fixed). |
| `main()` | argparse → `--port`, `--edid`, `--test-pattern`, `--test-period`. |

Key design choices:
- **Single client, simple semantics.** A `threading.Event` (`_dead`) is the only signal between the send path and the acceptor thread; once any send raises, the dead event fires, the acceptor closes the old socket and goes back into `accept()`. No queueing, no buffering beyond one frame in flight on the wire.
- **Per-frame fps logging on a 5-second sliding window** — gives a sense of activity without flooding logs at idle. Lets us spot fallback-vs-rect mode just from numbers (full-fallback runs dominated by 16.4 MB / frame).
- **`sku_area = 2560*1600`, `sku_pclk = sku_area * 60`** — these are the SKU area/pixel-clock limits libevdi takes in `connect()`. Setting them to native exactly accepts our preferred EDID timing.

---

## Step 4 — numpy

`python3-numpy 1.26.4` was already installed on this system (apt). `import numpy` works directly; no `--break-system-packages` install needed. Daemon imports `numpy as np`.

---

## Step 5 — Receiver-side protocol update

`android/app/src/main/java/com/tabdisplay/ReceiverActivity.java`. Diff vs Phase 2:

- New constants: `MAX_RECTS = 64`.
- `onCreate` now calls `surfaceView.getHolder().setFormat(PixelFormat.RGBA_8888)` before `addCallback` — fixes Phase 2's RGB_565 dither.
- New persistent fields in `ReceiverLoop`: `backing` (full-frame ARGB_8888 mirror of the virtual screen) + `backingCanvas` wrapping it, and a reusable `tempBmp` allocated at full frame size and `reconfigure()`d per rect so per-rect blits don't allocate.
- Inner loop:
  ```java
  int numRects = in.readInt();        // protocol header
  if (numRects < 1 || numRects > MAX_RECTS) break;   // reconnect
  for (int i = 0; i < numRects; i++) {
      readAndApplyRect(in);            // x1,y1,x2,y2,len,RGBA -> blit
  }
  present();                           // backing -> SurfaceHolder canvas
  ```
- `readAndApplyRect`: validates rect geometry + length == w*h*4 strictly; on mismatch it logs and `skipBytes(payloadLen)` to keep the stream aligned. On a valid rect it does:
  ```java
  tempBmp.reconfigure(rw, rh, Bitmap.Config.ARGB_8888);
  tempBmp.copyPixelsFromBuffer(ByteBuffer.wrap(payload, 0, payloadLen));
  backingCanvas.drawBitmap(tempBmp, x1, y1, null);
  ```
- `present()`: one `lockCanvas` → `drawBitmap(backing, 0, 0, null)` → `unlockCanvasAndPost`, per frame regardless of rect count. The rect blits happen on the backing bitmap, not the surface, so the surface only sees fully-composited frames.

Memory: backing 16 MB + temp 16 MB + payload 16 MB = 48 MB. Fits.

Build + install:
```
$ ./gradlew assembleDebug
BUILD SUCCESSFUL in 950ms
$ adb -s TABLET_SERIAL install -r app/build/outputs/apk/debug/app-debug.apk
Success
```

---

## Step 6 — Colour verification (test pattern via the swap)

The initial test-pattern implementation synthesised RGBA arrays directly and bypassed `xrgb_to_rgba` — it would have shown the right colours even if the swap function were broken. Rewrote it to build XRGB8888 (B,G,R,X memory) source frames and pass them through the real swap path. Now `--test-pattern` is a true byte-order test.

Run (7 s, app freshly relaunched first):
```
2026-06-03 05:28:05 INFO test-pattern: cycling 5 colours every 1.0s (via xrgb_to_rgba)
2026-06-03 05:28:05 INFO listening on 0.0.0.0:7070
2026-06-03 05:28:06 INFO [0] red     xrgb-in=(B=0,G=0,R=255)     rgba-out[0,0]=(255, 0, 0, 255)     sent=False  (no client yet)
2026-06-03 05:28:06 INFO client connected from ('127.0.0.1', 50623)
2026-06-03 05:28:07 INFO [1] green   xrgb-in=(B=0,G=255,R=0)     rgba-out[0,0]=(0, 255, 0, 255)     sent=True
2026-06-03 05:28:08 INFO [2] blue    xrgb-in=(B=255,G=0,R=0)     rgba-out[0,0]=(0, 0, 255, 255)     sent=True
2026-06-03 05:28:10 INFO [3] white   xrgb-in=(B=255,G=255,R=255) rgba-out[0,0]=(255, 255, 255, 255) sent=True
2026-06-03 05:28:11 INFO [4] magenta xrgb-in=(B=255,G=0,R=255)   rgba-out[0,0]=(255, 0, 255, 255)   sent=True
```

Tablet ended on magenta. User-confirmed: "Magenta / purple-pink". End-to-end byte order is correct.

---

## Step 7 — Real evdi end-to-end

```
$ PYTHONPATH=evdi-src/pyevdi python3 daemon.py
2026-06-03 05:29:16 INFO loaded EDID edid_2560x1600.bin (128 bytes)
2026-06-03 05:29:16 INFO root Opened /dev/dri/card0 as slave drm device
2026-06-03 05:29:16 INFO root LibEvdi version (1.14.2)
2026-06-03 05:29:16 INFO opened evdi card 0
2026-06-03 05:29:16 INFO evdi connected; pumping events (Ctrl-C to stop)
2026-06-03 05:29:17 INFO listening on 0.0.0.0:7070
2026-06-03 05:29:17 INFO root Got dpms signal: "0"
2026-06-03 05:29:17 INFO root Got mode_changed signal.
2026-06-03 05:29:17 INFO mode_changed: 2560x1600 @ 60 Hz, bpp=32, pixel_format=0x34325258
2026-06-03 05:29:17 INFO client connected from ('127.0.0.1', 40189)
```

Pixel format `0x34325258` is fourcc `"XR24"` = `DRM_FORMAT_XRGB8888`. Confirms the memory layout the swap function assumes.

GNOME's `org.gnome.Mutter.DisplayConfig.GetCurrentState` listed both `eDP-1` and `DVI-I-1`. Sysfs `card0-DVI-I-1/status` flipped to `connected`. User opened Settings → Displays, saw two side-by-side monitors, and dragging a window past the right edge of `eDP-1` brought it onto the tablet with correct colours.

### Throughput (excerpt of 5-second windows)
```
idle  : sent 5-6 frames in 5-6 s, ~1 fps, 0.1 MB total (dirty rects, mostly tiny)
drag  : sent 9-10 frames in 5 s, 1.8 fps, 49-114 MB   (heavy dirty area, mostly full-frame fallback)
flurry: sent 42 frames in 5.4 s, 7.7 fps, 172 MB      (sustained partial-rect updates)
```

Bandwidth ceiling matches Phase 2's 38 MB/s measurement: 16.4 MB ÷ 38 MB/s ≈ 430 ms per full-frame, capping us at ~2 fps for full-fallback bursts.

### `gpaste-daemon` crash (unrelated)
User reported a SIGSEGV notification for `gpaste-daemon` in `g_variant_new_tuple()` around the same time. `journalctl --user -p err --since "5 min ago"` returned no entries matching `gpaste` / `segv` for this session, and `pgrep -fla gpaste` finds no live gpaste process. Most likely: an old apport report being surfaced by Ubuntu's notification system, possibly poked into view by the GNOME display-config change. Not caused by the daemon, doesn't affect the pipeline.

---

## Decisions log (concise)

1. **Patched `pyevdi` to return `Buffer.bytes` as a real memoryview.** The upstream binding returns `void*`, which pybind11 wraps as an opaque capsule. Without this fix, daemon-side numpy work is impossible. Kept the patch local (in `evdi-src/pyevdi/PyEvdi.cpp`).
2. **Hand-coded EDID generator over a downloaded blob.** Lets us tune size/timing without re-running an online tool, and the source documents *why* each byte is what it is. CVT-RB picked over standard CVT (lower pixel clock 268.5 vs 348.5 MHz) — kinder to mutter and to range-limits.
3. **Test pattern reroutes through `xrgb_to_rgba`.** The brief's stated motivation for `--test-pattern` was "catches the byte-swap issue before it manifests on real content"; the original implementation synthesised RGBA directly and would have failed silently in a swap regression. Fix was a 6-line rewrite.
4. **80% threshold for full-frame fallback.** Round number; haven't benchmarked yet. At 50% we'd send more partial rects (smaller per-frame, but more rect headers + more per-rect bitmap reconfigures on the receiver). At 95% we'd send tiny rects more often (less helpful when mutter just sends one rect that covers 90%+ anyway). 80% is a reasonable starting point.
5. **Single backing Bitmap + Canvas in the receiver, with reusable tempBmp via `reconfigure()`.** No per-rect allocations means no GC pressure during continuous updates. 48 MB resident is well within the heap budget.
6. **`PixelFormat.RGBA_8888` on the SurfaceHolder.** Removes the SurfaceFlinger RGB_565 dither path. One line, real quality win.
7. **No `force_full` on reconnect.** mutter fires the framebuffer callback at idle (~1 Hz) with a full-screen rect, so within a second of reconnect the receiver gets a full update. Skipped the extra state machine.
8. **`run_test_pattern` bypasses evdi entirely.** Lets us validate the transport even with no monitor attached. Kept the same encode path (`encode_packet`) so the protocol is the same code-path in both modes.

---

## Artefacts on disk (handover to Phase 4)

| Path | Purpose |
|---|---|
| `~/projects/tabdisplay/edid_2560x1600.bin` | 128-byte EDID; mutter uses it for native 2560×1600@60 |
| `~/projects/tabdisplay/generate_edid.py` | EDID generator (re-run any time to regenerate) |
| `~/projects/tabdisplay/daemon.py` | the daemon — `python3 daemon.py [--test-pattern]` |
| `~/projects/tabdisplay/evdi-src/pyevdi/PyEvdi.cpp` | **patched**; `Buffer.bytes` now returns memoryview, `buffer_size` exposed |
| `~/projects/tabdisplay/evdi-src/pyevdi/PyEvdi.cpython-312-x86_64-linux-gnu.so` | rebuilt PyEvdi binding |
| `~/projects/tabdisplay/evdi-src/library/libevdi.so.1.14.2` | bundled libevdi (link target) |
| `~/projects/tabdisplay/android/app/src/main/java/com/tabdisplay/ReceiverActivity.java` | new multi-rect protocol + RGBA_8888 surface |
| `~/projects/tabdisplay/android/app/build/outputs/apk/debug/app-debug.apk` | rebuilt APK |
| `~/projects/tabdisplay/test_send_frame.py` | legacy single-frame sender — **uses the OLD single-len protocol**; if you need it back, run the v2 APK and downgrade the protocol, or refactor the script to wrap its frame with `b"\x00\x00\x00\x01" + <rect-header> + payload` (one full-frame rect). |
| `/tmp/tabdaemon.log` | last daemon run log |

---

## Open notes for Phase 4

- **Compression is the latency story.** Each 16.4 MB raw RGBA full-fallback frame ≈ 430 ms on the wire. JPEG at q=85 typically gives 10-20× ratio on desktop content → 0.8-1.6 MB / frame → 25-40 ms. Per-rect JPEG is the cleanest fit because the receiver already deals with rect blits — `BitmapFactory.decodeByteArray` per rect, then `drawBitmap` exactly as today. Header would add `[u32 codec]` per rect (0=raw RGBA, 1=JPEG).
- **`force_full` on reconnect would be a 10-line addition.** Currently the receiver starts with a black backing bitmap; the first dirty-rect packet leaves the rest black until mutter's next idle full-frame ~1 second later. For Phase 4 polish, daemon side: after accept, `force_full = True`; in the framebuffer callback, if the flag is set, ignore `buf.rects` and send `(0,0,W,H)`. Send the most recent RGBA copy if `acquire_framebuffer` hasn't fired yet (rare).
- **Audio is not a phase-4 concern.** evdi exposes no audio path. If we want audio mirroring later, it's a separate channel (pulse → ffmpeg → adb forward).
- **DPMS state.** Daemon logs `Got dpms signal: "0"` (display on) from libevdi. When mutter sleeps the virtual display (DPMS 3 = off), we should stop sending frames and possibly tell the receiver to dim or show a placeholder. Today the receiver just keeps the last frame on screen indefinitely.
- **Connector hysteresis on disconnect.** Mutter retains the connector briefly after `card.disconnect()`. Phase 4's bootstrap script should debounce daemon restarts (≥1 s) to avoid mutter racing.
- **Permissions for autostart.** `/dev/dri/card0` ACL is granted late by logind. A custom udev rule (`/etc/udev/rules.d/61-evdi.rules`) granting `video` would let a user-systemd unit start the daemon at login without waiting for ACL settling.
- **`screenOrientation="landscape"` picks *some* landscape.** If the user flips the tablet 180°, the picture inverts. `sensorLandscape` would auto-orient; but then the EDID's preferred timing (2560 wide × 1600 tall) flips relative to the panel, which mutter wouldn't notice. Probably fine as-is.
- **JNI critical-lock warnings**: still present in logcat ("JNI critical lock held for 17 ms"). Per-rect `copyPixelsFromBuffer` is heavy. Phase 4 could move to `ImageReader`/`HardwareBuffer` for zero-copy paths if compression alone isn't enough.
