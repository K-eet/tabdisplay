# tabdisplay

Android tablet as a second monitor for GNOME/Wayland over USB. Live pipeline:
`host/` (daemon + capture helper) and `android/`. `README.md` has layout,
run and test commands.

## Read first

- **`notes_2026-11-11.md`** — before changing anything in `host/` or
  `android/`, or when asked about latency, frame rate, the pointer, or
  "why is it built this way". Every odd-looking mechanism in `tdcap.c`,
  `scrub.py` and `DisplayActivity` is a workaround for a measured mutter 46
  or Android behaviour documented there, with the numbers. Its last section
  lists what is still open.

## Dead code

`daemon_v2.py`, `start.sh`, `tests/test_daemon.py`, `test_pyevdi.py`, the EDID
files, `patches/`, `evdi-src/` and the `phase*_log.md` files are the previous
evdi pipeline. The app no longer speaks their protocol. Treat them as history.

## Working here

- **Measure on the tablet.** A change to capture, encode, protocol or the app
  is done when the daemon's 5-second stats line under `bench/motion.py meta`
  load still reads 60.0 fps with latency no worse than the notes' table, and
  a tablet screenshot (`adb exec-out screencap -p`) shows what you expect.
  The view hierarchy and host logs have both been right while the screen was
  wrong.
- **Daemon and app ship together.** A wire change means bumping `VERSION` in
  both `host/tabdisplayd.py` and `Wire.java`, rebuilding, and reinstalling
  the APK.
- **The systemd service owns port 7070 while the tablet is plugged in.**
  Stop it (`systemctl --user stop tabdisplay`) before running the daemon by
  hand, and start it again afterwards.
- **`install.sh` is for the user to run.** It writes a udev rule and a
  systemd unit; ask rather than run it.
- Record new measurements and findings in the notes file, with the numbers.
