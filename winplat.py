"""casebot on Windows: the window, screen, text, mouse and keyboard calls.

casebot.py imports these in place of its macOS ones (the bottom of that file), so the
bot itself is the same code on both. Each name answers what its macOS twin answers:
global points from the top left, pixel boxes from the top left, and a capture of the
Roblox window's own pixels. The process is DPI aware, so a window box, its capture and
the pointer all count the same physical pixels (scale 1).

Text is read by RapidOCR, which ships its models inside the pip package: nothing to
install on Windows itself and nothing downloaded at run time.
"""

import ctypes
import math
import os
import threading
from collections import namedtuple
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageGrab

from ctypes import c_int32, c_uint16, c_uint32, c_size_t, c_void_p

# What the last capture said when it gave back nothing, for the log (casebot.GRAB_ERROR).
GRAB_ERROR: List[str] = [""]

Point = namedtuple("Point", "x y")


# --------------------------------------------------------------------------- images


def img_size(img) -> Tuple[int, int]:
    return img.size


def load_png(path: str):
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except OSError as exc:
        raise RuntimeError("cannot read image: %s" % path) from exc


def save_png(img, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")


def _box(img, x0, y0, x1, y1):
    """The box clamped to the image, or None when nothing of it is left."""
    W, H = img.size
    x0, y0 = max(0.0, float(x0)), max(0.0, float(y0))
    x1, y1 = min(float(x1), W), min(float(y1), H)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _sample(img, x0: float, y0: float, x1: float, y1: float, grid: int = 96):
    """Average (r, g, b) of one rectangle of a capture."""
    box = _box(img, x0, y0, x1, y1)
    if box is None:
        return (0.0, 0.0, 0.0)
    crop = img.crop((int(box[0]), int(box[1]), int(math.ceil(box[2])), int(math.ceil(box[3]))))
    r, g, b = np.asarray(crop, dtype=np.float32).reshape(-1, 3).mean(axis=0)
    return (float(r), float(g), float(b))


def _rgb_grid(img, x0: float, y0: float, x1: float, y1: float, cols: int, rows: int):
    """One region of a capture as rows x cols of (r, g, b), each cell averaged."""
    box = _box(img, x0, y0, x1, y1)
    if box is None:
        return []
    small = img.resize((max(1, int(cols)), max(1, int(rows))), Image.BOX, box=box)
    return [[tuple(px) for px in row] for row in np.asarray(small).tolist()]


def _luma_grid(img, x0: float, y0: float, x1: float, y1: float, cols: int, rows: int):
    """One region of a capture as rows x cols of brightness (0-255)."""
    box = _box(img, x0, y0, x1, y1)
    if box is None:
        return []
    small = img.resize((max(1, int(cols)), max(1, int(rows))), Image.BOX, box=box)
    return np.asarray(small, dtype=np.float32).mean(axis=2).tolist()


def _scaled_crop(img, box, k: float):
    """One region of a capture redrawn at scale k."""
    x0, y0, x1, y1 = box
    w, h = max(1, int((x1 - x0) * k)), max(1, int((y1 - y0) * k))
    box = _box(img, x0, y0, x1, y1)
    return img.resize((w, h), Image.LANCZOS, box=box) if box else None


# --------------------------------------------------------------------------- text


_ENGINE: list = []
_ENGINE_LOCK = threading.Lock()
# RapidOCR stretches any image narrower or shorter than this up to it, text and all.
DET_SIDE = 736


def read_text(img) -> List[Tuple[str, float, float, float, float, float]]:
    """(text, confidence, x, y, w, h) for each line of text, in the image's pixels."""
    with _ENGINE_LOCK:
        if not _ENGINE:
            from rapidocr import RapidOCR
            # no rotated text in this game, so no orientation pass
            _ENGINE.append(RapidOCR(params={"Global.log_level": "error", "Global.use_cls": False}))
    w, h = img.size
    if min(w, h) < DET_SIDE:
        # A close-up (the coin counter's strip, a basket price) goes on a canvas of
        # its own colour instead, top left so the boxes stay where they were. Stretched,
        # a counter of 4 or 111 read as nothing at every zoom; padded, it reads.
        bg = tuple(int(v) for v in np.asarray(img).reshape(-1, 3).mean(axis=0))
        canvas = Image.new("RGB", (max(w, DET_SIDE), max(h, DET_SIDE)), bg)
        canvas.paste(img, (0, 0))
        img = canvas
    out = _ENGINE[0](img)
    if out.boxes is None or out.txts is None:
        return []
    lines = []
    for box, text, score in zip(out.boxes, out.txts, out.scores):
        xs, ys = box[:, 0], box[:, 1]
        x, y = float(xs.min()), float(ys.min())
        lines.append((str(text), float(score), x, y, float(xs.max()) - x, float(ys.max()) - y))
    return lines


# --------------------------------------------------------------------------- input events
# Built without touching Windows, so they can be checked anywhere; only _send posts.


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", c_int32), ("dy", c_int32), ("mouseData", c_uint32),
                ("dwFlags", c_uint32), ("time", c_uint32), ("dwExtraInfo", c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", c_uint16), ("wScan", c_uint16), ("dwFlags", c_uint32),
                ("time", c_uint32), ("dwExtraInfo", c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", c_uint32), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_WHEEL, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x0800, 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
WHEEL_DELTA = 120       # one notch of a wheel

# Virtual keys for the only things the bot types: a price, and the keys that clear one.
DIGIT_KEYS = {str(d): 0x30 + d for d in range(10)}
KEY_A = 0x41
KEY_DELETE = 0x08       # Backspace, which is what the Mac's Delete key is
KEY_RIGHT = 0x27
# Their scan codes (US layout, which digits keep everywhere). A game may read either.
_SCAN = {0x30: 0x0B, 0x31: 0x02, 0x32: 0x03, 0x33: 0x04, 0x34: 0x05, 0x35: 0x06,
         0x36: 0x07, 0x37: 0x08, 0x38: 0x09, 0x39: 0x0A, KEY_A: 0x1E, KEY_DELETE: 0x0E,
         KEY_RIGHT: 0x4D}
_EXTENDED = {KEY_RIGHT}


def _point(x: float, y: float):
    return Point(float(x), float(y))


def _mouse_ev(kind: str, pt):
    """A left-button 'move', 'down' or 'up' INPUT. down and up land where the pointer is."""
    if kind == "move":
        vx, vy, vw, vh = _virtual_screen()
        dx = int(round((pt.x - vx) * 65535.0 / max(1, vw - 1)))
        dy = int(round((pt.y - vy) * 65535.0 / max(1, vh - 1)))
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(dx, dy, 0, flags, 0, 0)))
    flags = {"down": MOUSEEVENTF_LEFTDOWN, "up": MOUSEEVENTF_LEFTUP}[kind]
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))


def _wheel_ev(dy: int):
    """One notch: positive dy scrolls the content down, as on the Mac."""
    data = (-WHEEL_DELTA if dy > 0 else WHEEL_DELTA) & 0xFFFFFFFF
    return INPUT(INPUT_MOUSE, _INPUTUNION(mi=MOUSEINPUT(0, 0, data, MOUSEEVENTF_WHEEL, 0, 0)))


def _key_ev(keycode: int, down: bool, char: Optional[str] = None, flags: int = 0):
    """A real key press, virtual key and scan code both.

    char is ignored: Windows makes the character from the key itself, and a game's
    text box may skip an injected character that came with no key.
    """
    f = (KEYEVENTF_EXTENDEDKEY if keycode in _EXTENDED else 0) | (0 if down else KEYEVENTF_KEYUP)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(keycode, _SCAN.get(keycode, 0), f, 0, 0)))


# --------------------------------------------------------------------------- windows (the OS)


class RECT(ctypes.Structure):
    _fields_ = [("left", c_int32), ("top", c_int32), ("right", c_int32), ("bottom", c_int32)]


class POINT(ctypes.Structure):
    _fields_ = [("x", c_int32), ("y", c_int32)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", c_uint32), ("dwTime", c_uint32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", c_uint32), ("biWidth", c_int32), ("biHeight", c_int32),
                ("biPlanes", c_uint16), ("biBitCount", c_uint16), ("biCompression", c_uint32),
                ("biSizeImage", c_uint32), ("biXPelsPerMeter", c_int32),
                ("biYPelsPerMeter", c_int32), ("biClrUsed", c_uint32), ("biClrImportant", c_uint32)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", c_uint32), ("PageFaultCount", c_uint32)] + [
        (n, c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def _sig(fn, restype, *argtypes):
    # Handles are pointers on 64-bit Windows: ctypes' default int would cut them in half.
    fn.restype, fn.argtypes = restype, list(argtypes)


if os.name == "nt":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    kernel32 = ctypes.WinDLL("kernel32")
    dwmapi = ctypes.WinDLL("dwmapi")
    psapi = ctypes.WinDLL("psapi")
    P = ctypes.POINTER
    _sig(user32.GetTopWindow, c_void_p, c_void_p)
    _sig(user32.GetWindow, c_void_p, c_void_p, c_uint32)
    _sig(user32.GetForegroundWindow, c_void_p)
    _sig(user32.IsWindowVisible, ctypes.c_int, c_void_p)
    _sig(user32.IsIconic, ctypes.c_int, c_void_p)
    _sig(user32.GetWindowLongW, c_int32, c_void_p, ctypes.c_int)
    _sig(user32.GetWindowThreadProcessId, c_uint32, c_void_p, P(c_uint32))
    _sig(user32.GetWindowTextW, ctypes.c_int, c_void_p, ctypes.c_wchar_p, ctypes.c_int)
    _sig(user32.GetWindowRect, ctypes.c_int, c_void_p, P(RECT))
    _sig(user32.GetClientRect, ctypes.c_int, c_void_p, P(RECT))
    _sig(user32.ClientToScreen, ctypes.c_int, c_void_p, P(POINT))
    _sig(user32.SetForegroundWindow, ctypes.c_int, c_void_p)
    _sig(user32.BringWindowToTop, ctypes.c_int, c_void_p)
    _sig(user32.ShowWindow, ctypes.c_int, c_void_p, ctypes.c_int)
    _sig(user32.AttachThreadInput, ctypes.c_int, c_uint32, c_uint32, ctypes.c_int)
    _sig(user32.GetCursorPos, ctypes.c_int, P(POINT))
    _sig(user32.SetCursorPos, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    _sig(user32.SendInput, c_uint32, c_uint32, P(INPUT), ctypes.c_int)
    _sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    _sig(user32.GetLastInputInfo, ctypes.c_int, P(LASTINPUTINFO))
    _sig(user32.GetDC, c_void_p, c_void_p)
    _sig(user32.ReleaseDC, ctypes.c_int, c_void_p, c_void_p)
    _sig(user32.PrintWindow, ctypes.c_int, c_void_p, c_void_p, c_uint32)
    _sig(gdi32.CreateCompatibleDC, c_void_p, c_void_p)
    _sig(gdi32.CreateCompatibleBitmap, c_void_p, c_void_p, ctypes.c_int, ctypes.c_int)
    _sig(gdi32.SelectObject, c_void_p, c_void_p, c_void_p)
    _sig(gdi32.GetDIBits, ctypes.c_int, c_void_p, c_void_p, c_uint32, c_uint32, c_void_p,
         P(BITMAPINFOHEADER), c_uint32)
    _sig(gdi32.DeleteObject, ctypes.c_int, c_void_p)
    _sig(gdi32.DeleteDC, ctypes.c_int, c_void_p)
    _sig(kernel32.GetCurrentThreadId, c_uint32)
    _sig(kernel32.GetTickCount, c_uint32)
    _sig(kernel32.OpenProcess, c_void_p, c_uint32, ctypes.c_int, c_uint32)
    _sig(kernel32.CloseHandle, ctypes.c_int, c_void_p)
    _sig(kernel32.QueryFullProcessImageNameW, ctypes.c_int, c_void_p, c_uint32,
         ctypes.c_wchar_p, P(c_uint32))
    _sig(kernel32.GetCurrentProcess, c_void_p)
    _sig(kernel32.SetThreadExecutionState, c_uint32, c_uint32)
    _sig(dwmapi.DwmGetWindowAttribute, c_int32, c_void_p, c_uint32, c_void_p, c_uint32)
    _sig(psapi.GetProcessMemoryInfo, ctypes.c_int, c_void_p, P(PROCESS_MEMORY_COUNTERS), c_uint32)

    # Physical pixels everywhere: window boxes, captures and the pointer agree (scale 1).
    try:
        user32.SetProcessDpiAwarenessContext(c_void_p(-4))      # per monitor, v2
    except AttributeError:
        user32.SetProcessDPIAware()
    # Keep the PC and its screen awake while the bot runs: what caffeinate does on a Mac.
    # A sleeping screen captures black, and a locked one takes no clicks.
    kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)

GW_HWNDNEXT = 2
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_LAYERED = 0x00000020, 0x00080000
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
SW_RESTORE = 9


def _send(ev) -> None:
    user32.SendInput(1, ctypes.byref(ev), ctypes.sizeof(INPUT))


def _virtual_screen() -> Tuple[int, int, int, int]:
    """(x, y, width, height) of all the screens together."""
    return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))


def _pid_of(hwnd) -> int:
    pid = c_uint32(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _frame(hwnd) -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) of what is drawn, without Windows' invisible borders."""
    r = RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r),
                                    ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _windows() -> list:
    """Top-level windows a person can see and click, front to back."""
    out = []
    h = user32.GetTopWindow(None)
    while h:
        cloaked = c_int32(0)
        dwmapi.DwmGetWindowAttribute(h, DWMWA_CLOAKED, ctypes.byref(cloaked), 4)
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        click_through = ex & WS_EX_TRANSPARENT and ex & WS_EX_LAYERED   # overlays
        if (user32.IsWindowVisible(h) and not user32.IsIconic(h) and not cloaked.value
                and not click_through):
            out.append(h)
        h = user32.GetWindow(h, GW_HWNDNEXT)
    return out


def _exe_name(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, 0, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), c_uint32(1024)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return os.path.basename(buf.value).lower() if ok else ""
    finally:
        kernel32.CloseHandle(h)


def _client_box(hwnd) -> Tuple[int, int, int, int]:
    """(x, y, width, height) of the window's inside, title bar and borders left out."""
    r, p = RECT(), POINT(0, 0)
    user32.GetClientRect(hwnd, ctypes.byref(r))
    user32.ClientToScreen(hwnd, ctypes.byref(p))
    return p.x, p.y, r.right - r.left, r.bottom - r.top


def find_roblox() -> Tuple[int, int, dict]:
    """(pid, window handle, bounds dict with X/Y/Width/Height in pixels, top-left origin)."""
    title = ctypes.create_unicode_buffer(64)
    for h in _windows():
        user32.GetWindowTextW(h, title, 64)
        if title.value != "Roblox":
            continue
        pid = _pid_of(h)
        if not _exe_name(pid).startswith("roblox"):
            continue
        x, y, w, hh = _client_box(h)
        if w > 400:
            return pid, h, {"X": float(x), "Y": float(y), "Width": float(w), "Height": float(hh)}
    raise RuntimeError("Roblox window not found (is Roblox open, and not minimised?)")


def grab(hwnd):
    """The window's inside, even under other windows (not minimised), or None.

    PrintWindow with PW_RENDERFULLCONTENT asks the compositor for what the game drew.
    A game it cannot see comes back black, which is a failed capture here: the bot
    then reads the screen area instead while Roblox is on top (casebot Bot._grab).
    """
    GRAB_ERROR[0] = ""
    _, _, w, h = _client_box(hwnd)
    if w <= 0 or h <= 0:
        GRAB_ERROR[0] = "the window has no size"
        return None
    wdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(wdc)
    bmp = gdi32.CreateCompatibleBitmap(wdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    buf = ctypes.create_string_buffer(w * h * 4)
    try:
        ok = user32.PrintWindow(hwnd, mdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        gdi32.SelectObject(mdc, old)        # GetDIBits wants the bitmap out of the DC
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        rows = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0) if ok else 0
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, wdc)
    if rows != h:
        GRAB_ERROR[0] = "PrintWindow failed"
        return None
    img = Image.frombytes("RGB", (w, h), buf.raw, "raw", "BGRX")
    if max(hi for _, hi in img.resize((32, 18), Image.BOX).getextrema()) < 8:
        GRAB_ERROR[0] = "the window captured black"
        return None
    return img


def grab_region(bounds: dict):
    """The screen area the window covers: whatever is drawn there, on top."""
    GRAB_ERROR[0] = ""
    x, y = int(round(bounds["X"])), int(round(bounds["Y"]))
    box = (x, y, x + int(round(bounds["Width"])), y + int(round(bounds["Height"])))
    try:
        return ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
    except OSError as exc:
        GRAB_ERROR[0] = str(exc)
        return None


def notify(message: str) -> None:
    """Only the Mac's screen-recording outage calls this, and Windows has none."""


def _input_idle() -> float:
    """Seconds since the last mouse or keyboard input, the bot's own included."""
    li = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(li))
    return ((kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


_session_idle = _input_idle


def _peak_mb() -> float:
    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / (1024.0 * 1024.0)


def _active_pid() -> Optional[int]:
    """The app the person is in: the owner of the foreground window."""
    h = user32.GetForegroundWindow()
    return _pid_of(h) if h else None


def _topmost_pid_at(point: Tuple[float, float], ignore_pid: Optional[int] = None) -> Optional[int]:
    """Owner pid of the front-most window covering a global point: where a click lands."""
    x, y = point
    for h in _windows():
        left, top, right, bottom = _frame(h)
        if left <= x < right and top <= y < bottom:
            pid = _pid_of(h)
            if pid != ignore_pid:
                return pid
    return None


def _cursor_at() -> Tuple[float, float]:
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (float(p.x), float(p.y))


def _cursor_point():
    return Point(*_cursor_at())


def _warp(pt) -> None:
    user32.SetCursorPos(int(round(pt.x)), int(round(pt.y)))


def _app_for(pid: int):
    """The window to bring forward for that pid: the foreground one if it is theirs."""
    fg = user32.GetForegroundWindow()
    if fg and _pid_of(fg) == pid:
        return fg
    for h in _windows():
        if _pid_of(h) == pid:
            return h
    return None


def _activate(hwnd) -> None:
    """Bring a window to the front.

    Windows only lets the app in front hand the front to another, so this joins the
    front window's input queue for the switch, the way most automation tools do.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return
    me = kernel32.GetCurrentThreadId()
    them = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    joined = bool(them) and them != me and user32.AttachThreadInput(me, them, 1)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if joined:
            user32.AttachThreadInput(me, them, 0)
