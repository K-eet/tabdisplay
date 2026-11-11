#!/usr/bin/env python3
"""Move the real pointer by relative steps through a throwaway uinput mouse.

    sudo bench/pointer.py DX DY [steps] [seconds]

Test tool: Wayland offers no other way to put the pointer on the virtual
monitor from a script. Needs write access to /dev/uinput.
"""
import fcntl
import os
import struct
import sys
import time

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y, BTN_LEFT = 0, 1, 0x110
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502


def emit(fd, etype, code, value):
    os.write(fd, struct.pack("llHHi", 0, 0, etype, code, value))


def main():
    dx, dy = int(sys.argv[1]), int(sys.argv[2])
    steps = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    secs = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    for req, val in ((UI_SET_EVBIT, EV_KEY), (UI_SET_KEYBIT, BTN_LEFT),
                     (UI_SET_EVBIT, EV_REL), (UI_SET_RELBIT, REL_X),
                     (UI_SET_RELBIT, REL_Y)):
        fcntl.ioctl(fd, req, val)
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack(
        "HHHH80sI", 0x03, 0x1234, 0x5678, 1, b"tabdisplay test pointer", 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    time.sleep(0.6)                 # let the compositor pick the device up
    for i in range(steps):
        emit(fd, EV_REL, REL_X, dx * (i + 1) // steps - dx * i // steps)
        emit(fd, EV_REL, REL_Y, dy * (i + 1) // steps - dy * i // steps)
        emit(fd, EV_SYN, 0, 0)
        time.sleep(secs / steps)
    time.sleep(0.2)
    fcntl.ioctl(fd, UI_DEV_DESTROY)
    os.close(fd)


if __name__ == "__main__":
    main()
