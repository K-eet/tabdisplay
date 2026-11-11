# tabdisplay

Use an Android tablet as a second monitor for a GNOME/Wayland desktop, over USB.

```
mutter virtual monitor ──dmabuf──► host/tdcap (VAAPI H.264) ──pipe──► host/tabdisplayd.py
                                                                            │
tablet: MediaCodec + pointer overlay ◄── DisplayActivity ◄── adb reverse ◄── TCP :7070
```

Plug the tablet in: a monitor appears in Settings → Displays and the tablet
shows it. Unplug: the monitor goes away and its windows return to the laptop.

| Piece | What it does |
|---|---|
| `host/tabdisplayd.py` | Daemon: adb link, virtual monitor lifecycle, layout, stream relay, stats |
| `host/tdcap.c` | PipeWire screencast → VAAPI encoder, zero-copy; paces frames to 60 Hz |
| `host/scrub.py` | Forces a repaint to clear a mutter stale-pointer bug |
| `android/` | Receiver app (`DisplayActivity`, `Wire`) |
| `install.sh` | Builds everything, installs the udev rule and systemd user service |
| `bench/` | Load generator, fake client, capture probes used for the numbers below |
| `tests/test_host.py` | Host tests (no tablet, no compositor needed) |
| `notes_2026-11-11.md` | Why it is built this way: measurements and mutter quirks |

`daemon_v2.py`, `start.sh`, `tests/test_daemon.py`, the EDID files, `patches/`
and `evdi-src/` are the previous evdi-based pipeline. Nothing uses them any
more and the current app does not speak their protocol; they are kept only
until the new pipeline has had some use.

## Requirements

GNOME Shell 46 on Wayland, a GPU with VAAPI H.264 encode, PipeWire, Xwayland.

```sh
sudo apt-get install -y build-essential pkg-config adb ffmpeg \
    libpipewire-0.3-dev libavcodec-dev libavfilter-dev libavutil-dev libdrm-dev
```

Android app: JDK 17 (`/usr/lib/jvm/java-17-openjdk-amd64`) and the Android SDK.
Tablet: USB debugging on, this computer authorised.

## Install

```sh
./install.sh              # tablet plugged in
./install.sh --remove     # undo the service and udev rule
```

After that the service starts when this tablet (matched by USB serial) is
plugged in and stops when it is unplugged.
Logs: `journalctl --user -u tabdisplay -f`.

## Run by hand

```sh
make -C host
python3 host/tabdisplayd.py --serial <adb serial>
python3 host/tabdisplayd.py --help
```

Useful options: `--scale 1.75` (initial display scale), `--cursor embedded`
(pointer inside the video instead of drawn by the tablet), `--codec hevc`,
`--size 1920x1200` (render smaller, tablet scales up), `--rc vbr --bitrate 20000`.

Position and scale set in Settings → Displays are remembered in
`~/.config/tabdisplay/layout.json` and restored on the next plug.

Every 5 s the daemon logs frame rate, bitrate and the time from a frame being
captured to the tablet handing it to its display.

## Test

```sh
python3 -m unittest tests.test_host
cd android && JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew testDebugUnitTest
python3 host/tabdisplayd.py --no-adb & python3 bench/fakeclient.py 5   # end to end, no tablet
```

## Wire protocol (v3)

Both directions: `[u8 type][u32 LE length][payload]`. The message catalogue is
in the docstring of `host/tabdisplayd.py`; video, pointer and sprite records
are defined in `host/tdcap.c`. Daemon and app must be the same version; a
mismatch is refused at the hello.

The daemon listens on `127.0.0.1` only; the stream is unauthenticated.
