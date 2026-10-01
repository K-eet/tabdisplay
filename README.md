# tabdisplay

Use an Android tablet as an extended display for a Linux desktop, over USB.

```
mutter ──► evdi virtual display ──► daemon_v2.py ──► ffmpeg (h264_vaapi)
                                                          │
tablet: MediaCodec ◄── ReceiverActivity_v2 ◄── adb reverse ◄── TCP :7070
```

| Piece | What it does |
|---|---|
| `daemon_v2.py` | Owns the evdi card, feeds raw frames to ffmpeg, sends encoded frames to the tablet |
| `android/` | The receiver app (`ReceiverActivity_v2` + `FrameReader`) |
| `start.sh` | Arms `adb reverse`, relaunches the app, runs the daemon |
| `edid_2560x1600.bin`, `generate_edid.py` | The EDID the virtual display advertises |
| `patches/` | The one pyevdi change the daemon depends on |
| `tests/` | Sender tests |
| `phase*_log.md`, `review_*.md` | History: how it got here and why |

## Wire protocol

A plain TCP stream of `[u32 big-endian length][one H.264 access unit]`,
nothing else. Each AU is Annex B and starts with an AUD. The first AU after
every connect is SPS+PPS+IDR, and an IDR follows at least every 0.5 s of
wall-clock time. Fixed 2560×1600, one client at a time (a new connection
replaces the old one).

## Setup

Kernel module and build dependencies (Ubuntu 24.04):

```sh
sudo apt-get install -y dkms evdi-dkms libdrm-dev python3-pybind11 python3-dev ffmpeg
sudo modprobe evdi
```

pyevdi, with the local patch. `evdi-src/` is a plain clone of upstream at
`v1.14.2`; it is **not** a submodule, so a fresh checkout of this repo needs:

```sh
git clone --branch v1.14.2 https://github.com/DisplayLink/evdi.git evdi-src
git -C evdi-src am ../patches/0001-pyevdi-expose-framebuffer-as-memoryview.patch
make -C evdi-src/library
make -C evdi-src/pyevdi
```

Android app. Needs a JDK that ships `jlink` (JDK 17 here; the default
Java 21 on this machine is a JRE and fails the build):

```sh
cd android
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The daemon and the app must come from the same revision: the wire protocol
has no version negotiation.

## Run

```sh
./start.sh                      # ADB_SERIAL=<serial> to pick a tablet
./start.sh --no-auto-mode --bitrate 15M   # extra flags go to the daemon
python3 daemon_v2.py --help
```

The daemon listens on `127.0.0.1` only; the stream is unauthenticated.

## Test

```sh
python3 -m unittest discover -s tests            # sender
cd android && JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew testDebugUnitTest
```

The sender suite needs no tablet and no evdi device. Tests that drive the
real encoder are skipped without ffmpeg and `/dev/dri/renderD128`.
`test_pyevdi.py` is a manual hardware smoke test for the evdi binding; it
creates a real virtual display.
