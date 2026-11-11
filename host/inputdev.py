"""Virtual touchscreen and pen for the tablet, through /dev/uinput.

The tablet reports fingers and its stylus; this replays them as two kernel
input devices, so the desktop sees an ordinary touch monitor with a pen:
multitouch gestures, pressure, tilt, hover, the side button, the eraser.

GNOME decides which monitor a touchscreen or pen tablet belongs to from a
per-device setting keyed by USB vendor:product. `map_to_monitor` writes
that setting for our two devices before they are created.

Coordinates arrive as 0..65535 across the picture, which is also the axis
range of both devices; nothing is scaled here.

Needs write access to /dev/uinput (install.sh adds the udev rule). Without
it `open_devices` returns None and the display works without input.
"""
import fcntl
import logging
import os
import struct

from gi.repository import Gio, GLib

log = logging.getLogger("tabdisplay")

VENDOR = 0x1209             # pid.codes, the open-hardware vendor id
PRODUCT_TOUCH = 0x7401
PRODUCT_PEN = 0x7402
AXIS_MAX = 65535
PRESSURE_MAX = 4095
SLOTS = 10

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y = 0x00, 0x01
ABS_PRESSURE, ABS_TILT_X, ABS_TILT_Y = 0x18, 0x1a, 0x1b
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x2f, 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39
BTN_TOOL_PEN, BTN_TOOL_RUBBER = 0x140, 0x141
BTN_TOUCH, BTN_STYLUS = 0x14a, 0x14b
INPUT_PROP_DIRECT = 0x01    # the surface you touch is the display

UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_SET_ABSBIT, UI_SET_PROPBIT = 0x40045567, 0x4004556e
UI_DEV_SETUP, UI_ABS_SETUP = 0x405c5503, 0x401c5504
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502

# Pen flags on the wire.
PEN_IN_RANGE, PEN_TOUCHING, PEN_BUTTON, PEN_ERASER = 1, 2, 4, 8


class _Device:
    def __init__(self, name, product, keys, axes):
        """axes: {code: (min, max, resolution)}"""
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for key in keys:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, key)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for code, (lo, hi, res) in axes.items():
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack(
                    "HHiiiiii", code, 0, 0, lo, hi, 0, 0, res))
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack(
                "HHHH80sI", 0x03, VENDOR, product, 1, name.encode(), 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self._batch = []

    def put(self, etype, code, value):
        self._batch.append(struct.pack("llHHi", 0, 0, etype, code, value))

    def sync(self):
        self._batch.append(struct.pack("llHHi", 0, 0, EV_SYN, 0, 0))
        data, self._batch = b"".join(self._batch), []
        try:
            os.write(self.fd, data)
        except OSError:
            pass                # a full queue drops this report, not the link

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


class Touchscreen:
    def __init__(self, res_x, res_y):
        self.dev = _Device("tabdisplay touchscreen", PRODUCT_TOUCH, [BTN_TOUCH], {
            ABS_X: (0, AXIS_MAX, res_x), ABS_Y: (0, AXIS_MAX, res_y),
            ABS_MT_SLOT: (0, SLOTS - 1, 0),
            ABS_MT_TRACKING_ID: (0, 65535, 0),
            ABS_MT_POSITION_X: (0, AXIS_MAX, res_x),
            ABS_MT_POSITION_Y: (0, AXIS_MAX, res_y),
        })
        self.active = {}        # slot -> tracking id
        self.next_id = 0

    def update(self, points):
        """points: [(slot, down, x, y)], the tablet's current pointers. A
        slot that is active here but missing from the list is lifted, so a
        lost "up" cannot leave a finger stuck on the screen."""
        seen = set()
        first = None
        for slot, down, x, y in points:
            if not 0 <= slot < SLOTS:
                continue
            seen.add(slot)
            self.dev.put(EV_ABS, ABS_MT_SLOT, slot)
            if not down:
                if slot in self.active:
                    del self.active[slot]
                    self.dev.put(EV_ABS, ABS_MT_TRACKING_ID, -1)
                continue
            if slot not in self.active:
                self.active[slot] = self.next_id
                self.dev.put(EV_ABS, ABS_MT_TRACKING_ID, self.next_id)
                self.next_id = (self.next_id + 1) & 0xffff
            self.dev.put(EV_ABS, ABS_MT_POSITION_X, x)
            self.dev.put(EV_ABS, ABS_MT_POSITION_Y, y)
            if first is None:
                first = (x, y)
        for slot in [s for s in self.active if s not in seen]:
            del self.active[slot]
            self.dev.put(EV_ABS, ABS_MT_SLOT, slot)
            self.dev.put(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.dev.put(EV_KEY, BTN_TOUCH, 1 if self.active else 0)
        if first:
            self.dev.put(EV_ABS, ABS_X, first[0])
            self.dev.put(EV_ABS, ABS_Y, first[1])
        self.dev.sync()

    def release_all(self):
        if self.active:
            self.update([])

    def close(self):
        self.release_all()
        self.dev.close()


class Pen:
    def __init__(self, res_x, res_y):
        self.dev = _Device(
            "tabdisplay pen", PRODUCT_PEN,
            [BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS], {
                ABS_X: (0, AXIS_MAX, res_x), ABS_Y: (0, AXIS_MAX, res_y),
                ABS_PRESSURE: (0, PRESSURE_MAX, 0),
                # degrees; the kernel wants tilt resolution in units/radian
                ABS_TILT_X: (-90, 90, 57), ABS_TILT_Y: (-90, 90, 57),
            })
        self.tool = None        # BTN_TOOL_* currently in proximity

    def update(self, flags, x, y, pressure, tilt_x, tilt_y):
        in_range = bool(flags & PEN_IN_RANGE)
        tool = (BTN_TOOL_RUBBER if flags & PEN_ERASER else BTN_TOOL_PEN) \
            if in_range else None
        if tool != self.tool and self.tool is not None:
            # Leaving proximity, or the pen was flipped: finish the old
            # tool in its own report before the new one appears.
            self.dev.put(EV_KEY, BTN_TOUCH, 0)
            self.dev.put(EV_ABS, ABS_PRESSURE, 0)
            self.dev.put(EV_KEY, BTN_STYLUS, 0)
            self.dev.put(EV_KEY, self.tool, 0)
            self.dev.sync()
        self.tool = tool
        if tool is None:
            return
        touching = bool(flags & PEN_TOUCHING)
        level = pressure * PRESSURE_MAX // AXIS_MAX
        self.dev.put(EV_ABS, ABS_X, x)
        self.dev.put(EV_ABS, ABS_Y, y)
        self.dev.put(EV_ABS, ABS_TILT_X, max(-90, min(90, tilt_x)))
        self.dev.put(EV_ABS, ABS_TILT_Y, max(-90, min(90, tilt_y)))
        self.dev.put(EV_ABS, ABS_PRESSURE, max(1, level) if touching else 0)
        self.dev.put(EV_KEY, tool, 1)
        self.dev.put(EV_KEY, BTN_STYLUS, 1 if flags & PEN_BUTTON else 0)
        self.dev.put(EV_KEY, BTN_TOUCH, 1 if touching else 0)
        self.dev.sync()

    def close(self):
        self.update(0, 0, 0, 0, 0, 0)
        self.dev.close()


class Devices:
    def __init__(self, width_px, height_px, dpi):
        mm_x = max(1.0, width_px / max(dpi, 1) * 25.4)
        mm_y = max(1.0, height_px / max(dpi, 1) * 25.4)
        res_x, res_y = round(AXIS_MAX / mm_x), round(AXIS_MAX / mm_y)
        self.touch = Touchscreen(res_x, res_y)
        try:
            self.pen = Pen(res_x, res_y)
        except OSError:
            self.touch.close()
            raise

    def handle(self, kind, payload):
        """One 'F' (fingers) or 'N' (pen) message from the tablet."""
        if kind == "F" and payload:
            n = payload[0]
            if len(payload) >= 1 + 6 * n:
                self.touch.update([struct.unpack_from("<BBHH", payload, 1 + 6 * i)
                                   for i in range(n)])
        elif kind == "N" and len(payload) >= 9:
            self.pen.update(*struct.unpack_from("<BHHHbb", payload))

    def release_all(self):
        self.touch.release_all()
        self.pen.update(0, 0, 0, 0, 0, 0)

    def close(self):
        self.touch.close()
        self.pen.close()


def map_to_monitor(spec):
    """Tell GNOME both devices sit on the monitor `spec` =
    [vendor, product, serial]. Must run before the devices appear."""
    for schema, folder, product in (
            ("org.gnome.desktop.peripherals.touchscreen", "touchscreens",
             PRODUCT_TOUCH),
            ("org.gnome.desktop.peripherals.tablet", "tablets", PRODUCT_PEN)):
        path = "/org/gnome/desktop/peripherals/%s/%04x:%04x/" % (
            folder, VENDOR, product)
        settings = Gio.Settings.new_with_path(schema, path)
        settings.set_value("output", GLib.Variant("as", list(spec)))
    Gio.Settings.sync()


def open_devices(width_px, height_px, dpi, monitor_spec):
    """Create both devices, or return None when uinput is not available."""
    try:
        if monitor_spec:
            map_to_monitor(monitor_spec)
        devices = Devices(width_px, height_px, dpi)
    except (OSError, GLib.Error) as exc:
        log.warning("touch and pen input unavailable (%s); run ./install.sh "
                    "to grant access to /dev/uinput", exc)
        return None
    log.info("touch and pen devices created")
    return devices
