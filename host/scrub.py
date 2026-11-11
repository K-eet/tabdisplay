"""Make mutter repaint a rectangle of the screen.

Why this exists: mutter 46 draws the pointer on a virtual monitor as a
software overlay and remembers where it last drew it, so that spot can be
repainted when the pointer moves on. An overlay update that leaves the
rectangle unchanged (which happens as the pointer crosses onto or off the
monitor, or hangs over its edge) marks that memory invalid, and the next
move then leaves the old pointer image behind in the monitor's
framebuffer: a stale pointer that stays on the tablet until something
else happens to redraw there.

Nothing in mutter's D-Bus API asks for a redraw. Mapping a window does:
the compositor has to repaint whatever the window covers. So this maps a
fully transparent, click-through, unmanaged X11 window over the rectangle
for a moment and takes it away again. Nothing visible happens, apart from
the stale pointer going away.

Needs Xwayland (GNOME starts it on demand and ships it by default). If
there is no X display the scrubber does nothing.
"""
import ctypes as C
import logging
import threading
import time

log = logging.getLogger("tabdisplay")

_TRUE_COLOR = 4
_INPUT_OUTPUT = 1
_CW_BACK_PIXEL = 1 << 1
_CW_BORDER_PIXEL = 1 << 3
_CW_OVERRIDE_REDIRECT = 1 << 9
_CW_COLORMAP = 1 << 13
_ALLOC_NONE = 0
_SHAPE_INPUT = 2


class _XVisualInfo(C.Structure):
    _fields_ = [("visual", C.c_void_p), ("visualid", C.c_ulong),
                ("screen", C.c_int), ("depth", C.c_int), ("klass", C.c_int),
                ("red_mask", C.c_ulong), ("green_mask", C.c_ulong),
                ("blue_mask", C.c_ulong), ("colormap_size", C.c_int),
                ("bits_per_rgb", C.c_int)]


class _XSetWindowAttributes(C.Structure):
    _fields_ = [("background_pixmap", C.c_ulong),
                ("background_pixel", C.c_ulong),
                ("border_pixmap", C.c_ulong), ("border_pixel", C.c_ulong),
                ("bit_gravity", C.c_int), ("win_gravity", C.c_int),
                ("backing_store", C.c_int), ("backing_planes", C.c_ulong),
                ("backing_pixel", C.c_ulong), ("save_under", C.c_int),
                ("event_mask", C.c_long), ("do_not_propagate_mask", C.c_long),
                ("override_redirect", C.c_int), ("colormap", C.c_ulong),
                ("cursor", C.c_ulong)]


def _load():
    x = C.CDLL("libX11.so.6")
    xf = C.CDLL("libXfixes.so.3")
    p, ul, i, ui = C.c_void_p, C.c_ulong, C.c_int, C.c_uint
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = p, [C.c_char_p]
    x.XDefaultScreen.restype, x.XDefaultScreen.argtypes = i, [p]
    x.XRootWindow.restype, x.XRootWindow.argtypes = ul, [p, i]
    x.XMatchVisualInfo.restype = i
    x.XMatchVisualInfo.argtypes = [p, i, i, i, C.POINTER(_XVisualInfo)]
    x.XCreateColormap.restype = ul
    x.XCreateColormap.argtypes = [p, ul, p, i]
    x.XCreateWindow.restype = ul
    x.XCreateWindow.argtypes = [p, ul, i, i, ui, ui, ui, i, ui, p, ul,
                                C.POINTER(_XSetWindowAttributes)]
    for name in ("XMapWindow", "XUnmapWindow", "XDestroyWindow"):
        getattr(x, name).restype = i
        getattr(x, name).argtypes = [p, ul]
    x.XFlush.restype, x.XFlush.argtypes = i, [p]
    x.XSync.restype, x.XSync.argtypes = i, [p, i]
    x.XCloseDisplay.restype, x.XCloseDisplay.argtypes = i, [p]
    xf.XFixesCreateRegion.restype = ul
    xf.XFixesCreateRegion.argtypes = [p, p, i]
    xf.XFixesSetWindowShapeRegion.restype = None
    xf.XFixesSetWindowShapeRegion.argtypes = [p, ul, i, i, i, ul]
    xf.XFixesDestroyRegion.restype = None
    xf.XFixesDestroyRegion.argtypes = [p, ul]
    return x, xf


class Scrubber:
    """`scrub(x, y, w, h)` in the compositor's logical coordinates.

    Calls are cheap to make in bursts: they collapse into one repaint, and
    the work happens on a private thread, never on the caller's.
    """

    HOLD = 0.06     # seconds the window stays mapped: a few frames

    def __init__(self):
        self._want = None
        self._cond = threading.Condition()
        self._ok = None         # None: not tried yet
        threading.Thread(target=self._run, name="scrub", daemon=True).start()

    def scrub(self, x, y, w, h, delay=0.0):
        with self._cond:
            due = time.monotonic() + delay
            if self._want is not None:
                due = min(due, self._want[4])
            self._want = (int(x), int(y), int(w), int(h), due)
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while self._want is None:
                    self._cond.wait()
                wait = self._want[4] - time.monotonic()
                if wait > 0:
                    self._cond.wait(wait)
                    continue
                x, y, w, h, _ = self._want
                self._want = None
            try:
                self._flash(x, y, w, h)
            except Exception as exc:        # never take the daemon down
                if self._ok is not False:
                    log.warning("cannot force a repaint (%s); a stale "
                                "pointer image may linger on the tablet", exc)
                self._ok = False

    def _flash(self, x, y, w, h):
        if self._ok is False or w <= 0 or h <= 0:
            return
        x11, xfixes = _load()
        dpy = x11.XOpenDisplay(None)
        if not dpy:
            raise RuntimeError("no X display (Xwayland not running)")
        try:
            screen = x11.XDefaultScreen(dpy)
            root = x11.XRootWindow(dpy, screen)
            vinfo = _XVisualInfo()
            if not x11.XMatchVisualInfo(dpy, screen, 32, _TRUE_COLOR,
                                        C.byref(vinfo)):
                raise RuntimeError("no 32-bit visual")
            attrs = _XSetWindowAttributes()
            attrs.background_pixel = 0          # transparent black
            attrs.border_pixel = 0
            attrs.override_redirect = 1         # the WM leaves it alone
            attrs.colormap = x11.XCreateColormap(dpy, root, vinfo.visual,
                                                 _ALLOC_NONE)
            win = x11.XCreateWindow(
                dpy, root, x, y, w, h, 0, 32, _INPUT_OUTPUT, vinfo.visual,
                _CW_BACK_PIXEL | _CW_BORDER_PIXEL | _CW_OVERRIDE_REDIRECT |
                _CW_COLORMAP, C.byref(attrs))
            # An empty input region: clicks fall through while it is up.
            region = xfixes.XFixesCreateRegion(dpy, None, 0)
            xfixes.XFixesSetWindowShapeRegion(dpy, win, _SHAPE_INPUT, 0, 0,
                                              region)
            xfixes.XFixesDestroyRegion(dpy, region)
            x11.XMapWindow(dpy, win)
            x11.XSync(dpy, 0)
            time.sleep(self.HOLD)
            x11.XUnmapWindow(dpy, win)
            x11.XDestroyWindow(dpy, win)
            x11.XSync(dpy, 0)
            self._ok = True
        finally:
            x11.XCloseDisplay(dpy)
