# Phase 2 Log — Android receiver app

**Target:** A minimal Android app that opens a TCP socket, reads raw RGBA frames from the Linux host, and renders them fullscreen to a SurfaceView. End-to-end smoke test: a Python script on the host sends one solid-red frame and the tablet's screen turns red.

**Tablet:** Samsung Galaxy Tab S7 FE — native 2560×1600, ADB serial `TABLET_SERIAL`. Currently **not** plugged in for this session.

**Status at end of session:** **PHASE 2 COMPLETE.** APK built, deployed, launched, reverse tunnel active, host→tablet pipe verified end-to-end: a single 2560×1600 solid-red RGBA frame (16.4 MB) was sent in 426 ms (≈38 MB/s) and the tablet rendered it.

One bug was found and fixed during deployment — see "Step 6 — Deployment + bug fix" below.

---

## Critical clarification — server vs. client direction

The brief specifies:
- the Android app "connects to localhost:7070" (client),
- the Python test script "Connects to localhost:7070" (also called "client").

This is internally inconsistent: with `adb reverse tcp:7070 tcp:7070`, the **device** opens a client socket to its own localhost:7070, and adb forwards that to the **host's** localhost:7070 — which means the host must be the *server*. If both sides were clients, nobody would accept.

**Implemented direction:**
- Host (`test_send_frame.py`) — TCP **server** on `0.0.0.0:7070`, accepts one client and sends one frame.
- Tablet (`ReceiverActivity.java`) — TCP **client**, connects to `localhost:7070`, reads frames.

Phase 3's pyevdi daemon will be the long-lived server on the host. This direction also has a nice operational property: the host can be restarted independently and the tablet just reconnects in a 1-second retry loop.

---

## Step 1 — Toolchain probe

```
java       : not installed
javac      : not installed
gradle     : not installed
adb        : /usr/bin/adb (apt 'android-sdk-platform-tools-common', adb 1.0.41 v34.0.4-debian)
ANDROID_HOME / ANDROID_SDK_ROOT : unset
/usr/lib/android-sdk : only platform-tools/, no platforms/ or build-tools/
adb devices: (empty) — tablet not connected
python3 + socket/struct: ok
```

Need to install: JDK 17, Gradle, Android SDK (platforms;android-34, build-tools;34.0.0).

---

## Step 2 — Install toolchain

```bash
sudo apt install -y openjdk-17-jdk-headless unzip      # JDK 17.0.19

# Gradle 8.7 (binary distribution, ~128 MB)
curl -sSLO https://services.gradle.org/distributions/gradle-8.7-bin.zip
unzip -q gradle-8.7-bin.zip -d ~/projects/tabdisplay/tools/

# Android cmdline-tools (~146 MB)
curl -sSLO https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
mkdir -p ~/Android/Sdk/cmdline-tools/.tmp
cd ~/Android/Sdk/cmdline-tools/.tmp
unzip -q .../commandlinetools-linux-11076708_latest.zip
mv cmdline-tools ../latest

export ANDROID_HOME=/home/user/Android/Sdk
export PATH=$ANDROID_HOME/cmdline-tools/latest/bin:$PATH

yes | sdkmanager --licenses           # accept all SDK licenses
sdkmanager --install 'platforms;android-34' 'build-tools;34.0.0' 'platform-tools'
```

Verified afterwards:

```
$ ls $ANDROID_HOME
build-tools  cmdline-tools  licenses  platforms  platform-tools
$ ls $ANDROID_HOME/platforms
android-34
$ ls $ANDROID_HOME/build-tools
34.0.0
$ $ANDROID_HOME/build-tools/34.0.0/aapt2 version
Android Asset Packaging Tool (aapt) 2.19-10229193
```

---

## Step 3 — Project layout

```
~/projects/tabdisplay/android/
├── settings.gradle              # plugin & dep resolution config
├── build.gradle                 # root: declares AGP 8.4.2
├── gradle.properties            # JVM heap, useAndroidX=true, parallel/caching
├── local.properties             # sdk.dir=/home/user/Android/Sdk
├── gradlew, gradlew.bat         # wrapper scripts (generated)
├── gradle/wrapper/
│   ├── gradle-wrapper.jar
│   └── gradle-wrapper.properties
└── app/
    ├── build.gradle             # android { compileSdk 34, minSdk 30, targetSdk 34 }
    └── src/main/
        ├── AndroidManifest.xml
        └── java/com/tabdisplay/ReceiverActivity.java
```

### Versioning decisions

| Component | Choice | Rationale |
|---|---|---|
| `compileSdk` | 34 | Latest stable platform we installed |
| `minSdk` | 30 | Tab S7 FE shipped on Android 11 (API 30); One UI is now API 33+; this skips the deprecated `View.SYSTEM_UI_FLAG_*` path almost entirely |
| `targetSdk` | 34 | Required by brief ("API 31+") and keeps us aligned with current behaviour |
| AGP | 8.4.2 | Compatible with Gradle 8.7 and JDK 17 |
| Gradle wrapper | 8.7 | Stable, current at install time |
| Java only | yes | Brief mandates |
| No third-party deps | yes | No `androidx.*`, no support libs; standard SDK only |

### AndroidManifest essentials

- `<uses-permission android:name="android.permission.INTERNET" />`
- Activity attributes: `screenOrientation="landscape"`, `configChanges="orientation|screenSize|keyboardHidden"` (prevents activity restart on rotation/IME flip), `keepScreenOn="true"`, `theme="@android:style/Theme.NoTitleBar.Fullscreen"`.
- `<intent-filter>` with `MAIN` + `LAUNCHER` (`android:exported="true"` required since API 31).
- `allowBackup="false"` — no need for OS-managed backup of an ephemeral receiver.

### ReceiverActivity.java highlights

- `WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON` (belt-and-braces alongside the manifest attribute).
- `WindowInsetsController.hide(statusBars | navigationBars)` for API 30+ immersive mode, with `BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE`. Legacy `SYSTEM_UI_FLAG_*` fallback is present but unreachable at `minSdk 30`.
- SurfaceView fills the screen (`setContentView(surfaceView)`); orientation is locked landscape in the manifest so the surface is always 2560×1600 on this device.
- `SurfaceHolder.Callback`: `surfaceCreated` spawns a daemon `frame-rx` thread; `surfaceDestroyed` flips `running=false` and interrupts.
- Receiver loop:
  - Reusable `byte[16,384,000]` payload buffer and a single reusable `Bitmap(2560, 1600, ARGB_8888)` — *no per-frame allocations* (important for Phase 4 latency).
  - `DataInputStream.readInt()` for the 4-byte big-endian header (`readInt` is BE-only in Java).
  - Hard length sanity check: `len > FRAME_BYTES * 2` triggers reconnect (resync). `len > buffer` skips and continues. Mismatch on the exact frame size logs and skips.
  - `bitmap.copyPixelsFromBuffer(ByteBuffer.wrap(payload))` → `holder.lockCanvas` → `canvas.drawBitmap` → `unlockCanvasAndPost`.
  - On socket error, sleeps 1 s and reconnects — graceful tolerance for host-side daemon restarts.

### Pixel-format note

Android `Bitmap.Config.ARGB_8888` with `copyPixelsFromBuffer` interprets the byte stream as `R, G, B, A` in memory order. Our Python frame is exactly `bytes([R, G, B, A, …])` — no byte swap needed. (If colours come out as blue-tinted, the swap would happen here.)

---

## Step 4 — Build

```bash
export ANDROID_HOME=/home/user/Android/Sdk
cd ~/projects/tabdisplay/android
gradle wrapper --gradle-version 8.7 --distribution-type bin    # one-time bootstrap
./gradlew assembleDebug
```

Output:

```
> Task :app:compileDebugJavaWithJavac
Note: ReceiverActivity.java uses or overrides a deprecated API.
Note: Recompile with -Xlint:deprecation for details.
...
BUILD SUCCESSFUL in 7s
33 actionable tasks: 33 executed
```

(The deprecation note is for the legacy `View.SYSTEM_UI_FLAG_*` fallback branch which is unreachable at `minSdk 30`. Not actionable.)

APK:
```
app/build/outputs/apk/debug/app-debug.apk    (12,885 bytes)
```

---

## Step 5 — Test sender (`test_send_frame.py`)

Listens on `0.0.0.0:7070`, accepts one client, sends one `2560×1600` RGBA frame, exits.

```bash
python3 test_send_frame.py                    # default: solid red
python3 test_send_frame.py --color blue       # also: green, white, black, magenta
python3 test_send_frame.py --width 1280 --height 720 --color green   # smaller test
```

Validated end-to-end (without the tablet) by importing `build_frame` and asserting:
- frame size = 2560 × 1600 × 4 = **16,384,000 bytes**
- first pixel: `ff 00 00 ff` (R G B A — red opaque)

---

## Step 6 — Deployment + bug fix

### Tablet reconnected
```
$ adb devices -l
TABLET_SERIAL   device usb:3-1 product:gts7fewifixx model:SM_T733 device:gts7fewifi
$ lsusb | grep -i samsung
Bus 003 Device 006: ID 04e8:6860 Samsung Electronics Co., Ltd Galaxy series, misc. (MTP mode)
```

### Install + first launch attempt — crashed
```
$ adb install -r app-debug.apk             → Success
$ adb shell am start -W -n com.tabdisplay/.ReceiverActivity
Status: ok  ... WaitTime: 182
```
But `dumpsys activity activities` immediately showed `topResumedActivity = com.samsung.android.lool/AppErrorDialogActivity` — Samsung's "X has stopped" intercept.

### Root-cause from logcat
```
E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: java.lang.RuntimeException: Unable to start activity ComponentInfo{com.tabdisplay/com.tabdisplay.ReceiverActivity}:
E AndroidRuntime:   java.lang.NullPointerException: Attempt to invoke virtual method
E AndroidRuntime:   'android.view.WindowInsetsController com.android.internal.policy.DecorView.getWindowInsetsController()'
E AndroidRuntime:   on a null object reference
E AndroidRuntime:   at com.android.internal.policy.PhoneWindow.getInsetsController(PhoneWindow.java:4155)
E AndroidRuntime:   at com.tabdisplay.ReceiverActivity.hideSystemBars(ReceiverActivity.java:53)
E AndroidRuntime:   at com.tabdisplay.ReceiverActivity.onCreate(ReceiverActivity.java:42)
```

**Cause:** `hideSystemBars()` was called *before* `setContentView()`. On Samsung One UI (and on stock Android), `Window.getDecorView()` (and hence `getInsetsController()`) returns null until `setContentView()` instantiates the decor view. `PhoneWindow.getInsetsController()` does `getDecorView().getWindowInsetsController()` and NPEs on the inner deref.

### Fix (ReceiverActivity.java onCreate)
Moved `hideSystemBars()` to *after* `setContentView()`:
```java
super.onCreate(savedInstanceState);
getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
surfaceView = new SurfaceView(this);
surfaceView.getHolder().addCallback(this);
setContentView(surfaceView);
// Must run AFTER setContentView so the decor (and its insets controller) exists.
hideSystemBars();
```

### Rebuild + relaunch — clean
```
./gradlew assembleDebug                 → BUILD SUCCESSFUL in 1s (incremental)
adb install -r app-debug.apk            → Success
adb logcat -c
adb shell am start -W -n com.tabdisplay/.ReceiverActivity
  Status: ok / LaunchState: COLD / TotalTime: 500
```
`dumpsys activity activities` then showed
`topResumedActivity = com.tabdisplay/.ReceiverActivity` — app is alive and on-screen.

Logcat highlights confirming the app is healthy:
```
I InsetsController: cancelAnimation: types=statusBars navigationBars, host=com.tabdisplay/...
D SurfaceFlinger: SOLID_COLOR | ... | Background for SurfaceView[com.tabdisplay.ReceiverActivity]@0#642
W tabdisplay: rx loop error: ... ECONNREFUSED — retrying in 1s
```
i.e. immersive mode triggered, SurfaceView created (initial black background), and the receiver thread is in its connect-retry loop — exactly the expected idle state before the host comes online.

---

## Step 7 — Reverse tunnel + end-to-end red frame

```
$ adb -s TABLET_SERIAL reverse tcp:7070 tcp:7070
7070
$ adb -s TABLET_SERIAL reverse --list
UsbFfs tcp:7070 tcp:7070

$ python3 ~/projects/tabdisplay/test_send_frame.py
[server] listening on 0.0.0.0:7070 (frame 16,384,000 bytes)
[hint] make sure 'adb reverse tcp:7070 tcp:7070' is active
[server] client connected from ('127.0.0.1', 44607)
[server] sent 16.4 MB in 426 ms (38 MB/s) — tablet should now show the colour
```

Tablet-side logcat at the same moment:
```
I tabdisplay: connected to localhost:7070
W com.tabdisplay: JNI critical lock held for 17.232ms on Thread "frame-rx"      ← copyPixelsFromBuffer
I SurfaceView: Locking canvas... stopped=false, surfaceControl=SurfaceView[...]
I BLASTBufferQueue: onFrameAvailable the first frame is available
D SurfaceFlinger: CLIENT | RGB_565 | 0.0 0.0 2560.0 1600.0 | SurfaceView[...](BLAST)#641
W tabdisplay: rx loop error: null — retrying in 1s                              ← script exited; expected
```

**Tablet screen displayed solid red.** Confirmed by surface composition trace and visual confirmation. The last-drawn frame persists on the SurfaceView after the socket closes, so the screen stays red while the receiver retries.

Throughput note: 38 MB/s sustained over the adb-reverse pipe. That's ~300 Mbps — comfortable for sparse updates but far below the 940 MB/s needed for raw-RGBA 60 Hz. Compression in Phase 4.

Pixel format note: SurfaceFlinger reported `RGB_565` for our SurfaceView. The ARGB_8888 bitmap got dithered down because we didn't call `holder.setFormat(PixelFormat.RGBA_8888)`. Solid red looks identical in either format, but for full-colour content in Phase 4 we'll need to set the surface format explicitly.

---

## Final verification (success criteria from the brief)

| Criterion | Result |
|---|---|
| APK installs and launches on tablet without crashing | **PASS** (after one bug fix) — `topResumedActivity = com.tabdisplay/.ReceiverActivity`, cold launch 500 ms |
| adb reverse tunnel is confirmed active | **PASS** — `adb reverse --list: UsbFfs tcp:7070 tcp:7070` |
| Running `test_send_frame.py` turns the tablet screen solid red | **PASS** — SurfaceFlinger composited the frame at 2560×1600, tablet shows red |
| `phase2_log.md` documents the full session | **PASS** — this file |

### Observed but accepted: black-on-refocus

After the red frame painted, backgrounding and refocusing the app showed a black screen instead of red. Root cause: `surfaceDestroyed` tears down the receiver thread and the Surface; refocus creates a *new* Surface (always black) and a *new* receiver thread, but `test_send_frame.py` already exited so nothing reconnects.

**Decision:** leave as-is. Phase 3's persistent daemon will be a long-running server, so each `surfaceCreated` reconnect will be answered with the current frame. Patching this now (looping the test sender, or caching the last bitmap in the Activity) would only be useful for demo isolation and would be redundant once Phase 3 lands.

---

## Decisions log (concise)

1. **Host=server, tablet=client** — disambiguated the brief's contradiction; matches `adb reverse` semantics; lets the daemon restart without disrupting the tablet beyond a 1 s reconnect.
2. **AGP 8.4.2 + Gradle 8.7 + JDK 17** — modern, compatible triad; AGP 8.5+ would require Gradle 8.7+ already, this combination is the conservative pick.
3. **`compileSdk=34, minSdk=30`** — modern API for `WindowInsetsController` immersive mode; minSdk 30 matches the device's original Android 11 baseline.
4. **No `androidx.*`** — brief mandates standard SDK only; `android.app.Activity` is sufficient.
5. **`Theme.NoTitleBar.Fullscreen`** — built-in theme that suppresses the action bar from the start; complements the runtime `WindowInsetsController` immersive call.
6. **`configChanges="orientation|screenSize|keyboardHidden"`** — prevents activity recreate on orientation change (even though screen is locked landscape, configChanges fires for screenSize on multi-window or DisplayCutout changes).
7. **Single reusable `byte[]` + `Bitmap`** in the receiver — eliminates per-frame allocations; ground-truthing Phase 4 will appreciate the lack of GC churn.
8. **`adb reverse` over `adb forward`** — `adb forward` exposes a device port to the host (host=client). We need the inverse so the daemon (host) doesn't have to discover the tablet's IP.
9. **No NDK / no native code in Phase 2** — Java bitmap blit is fine for 1 frame and decent for 60 Hz; latency profiling and possibly `ImageReader` + `SurfaceTexture` path come in Phase 4.
10. **`Bitmap.Config.ARGB_8888`** — only viable config for raw RGBA from `copyPixelsFromBuffer`; `RGB_565` would halve bandwidth but distort colours unless we re-pack on the sender, which we'll evaluate in Phase 4 against a compression decision.

---

## Artefacts on disk (handover to next session / Phase 3)

| Path | Purpose |
|---|---|
| `~/projects/tabdisplay/android/` | Gradle Android project |
| `~/projects/tabdisplay/android/app/build/outputs/apk/debug/app-debug.apk` | built debug APK (12,885 B) |
| `~/projects/tabdisplay/android/app/src/main/java/com/tabdisplay/ReceiverActivity.java` | the receiver |
| `~/projects/tabdisplay/android/app/src/main/AndroidManifest.xml` | manifest |
| `~/projects/tabdisplay/tools/gradle-8.7/` | extracted Gradle 8.7 (used to bootstrap wrapper) |
| `~/projects/tabdisplay/dl/` | downloaded zips (can be deleted: gradle-8.7-bin.zip, commandlinetools-linux-11076708_latest.zip) |
| `~/Android/Sdk/` | Android SDK (platforms;android-34, build-tools;34.0.0, platform-tools, cmdline-tools/latest) |
| `~/projects/tabdisplay/test_send_frame.py` | host-side TCP server / one-shot solid-colour sender |
| `/usr/lib/jvm/java-17-openjdk-amd64/` | JDK 17.0.19 (apt) |

---

## Open notes for Phase 3 / Phase 4

- **Frame allocation strategy**: the receiver reuses one `byte[]` and one `Bitmap` — for Phase 3 the daemon should likewise reuse a buffer and avoid copying through Python `bytes` repeatedly (use `bytearray`, `memoryview`, or `PIL.Image.frombuffer` if a stride-aware path is needed).
- **Compression in Phase 4**: at 2560×1600×4×60 ≈ 0.94 GB/s, raw RGBA exceeds even USB 3 worst-case throughput. Options: JPEG (cheap, lossy, GPU-decodable via `BitmapFactory`), VP8/H.264 (much better ratio, much higher decode latency), or simple per-tile dirty-rect with run-length encoding (which maps cleanly onto pyevdi's `Buffer.rects` field we observed in Phase 1).
- **Pyevdi → socket plumbing**: in Phase 1 our pyevdi test received `rect_count: 1` with one full-screen rect at the desktop idle rate (~2 Hz). When the daemon copies pyevdi's framebuffer to the socket, the dirty-rect list will drive partial updates, lowering bandwidth in real use.
- **Colour-format pitfall to watch for in Phase 3**: pyevdi gives us the raw framebuffer in whatever pixel format mutter requests — usually `XRGB8888` (alpha unused) in little-endian, which on the wire is `B G R X` byte order. We may need a byte swap before sending — verify with a coloured test pattern (the magenta colour in `test_send_frame.py` is good for this since pure R and pure B make the channel ordering obvious).
- **`adb reverse` lifecycle**: each `adb` reverse rule lives until `adb kill-server` or `adb reverse --remove tcp:7070`. Phase 4's bootstrap script should re-run `adb reverse tcp:7070 tcp:7070` on startup (idempotent) and tear it down on exit.
- **`screenOrientation="landscape"`**: locks to *some* landscape; the system picks. If the tablet is held in "reverse landscape" (USB on the wrong side), the picture will be rotated 180°. Switch to `sensorLandscape` if you want the user to be able to flip — but be aware of the EDID/coordinate-system implications upstream in pyevdi.
- **`logcat -s tabdisplay`**: my chosen TAG. Filter to that in Phase 3/4 to see connect / surfaceChanged / frame-loop logs.
- **`setTcpNoDelay(true)`** on both the Java side and Python side — small win on first-byte latency for the header read. Don't set `SO_KEEPALIVE` until Phase 4 when we care about half-open detection.
