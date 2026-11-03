"""Thin ctypes layer over user32/shcore/dwmapi for things pywinauto does not
expose directly (DPI awareness, monitor DPI, z-order, cloaking, display
affinity, busy cursor). Import only on Windows.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from .errors import PlatformUnsupported
from .models import Rect

if sys.platform != "win32":  # pragma: no cover - guarded import
    raise PlatformUnsupported("platform_win is only available on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:  # Windows 7
    shcore = None
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:
    dwmapi = None

for _fn in ("GetForegroundWindow", "GetWindow", "GetAncestor"):
    getattr(user32, _fn).restype = wintypes.HWND
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
kernel32.GetConsoleWindow.restype = wintypes.HWND
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]

GW_OWNER = 4
GA_ROOTOWNER = 3
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_MINIMIZE = 0x20000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
DWMWA_CLOAKED = 14
IDC_WAIT = 32514
IDC_APPSTARTING = 32650
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def _rect(r: wintypes.RECT) -> Rect:
    return Rect(r.left, r.top, r.right, r.bottom)


# --------------------------------------------------------------------------- DPI

def enable_dpi_awareness() -> str:
    """Make this process per-monitor DPI aware so that window rectangles and
    screen captures are both in physical pixels. Must run before any GUI
    library creates windows or caches metrics (the CLI does this first)."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "per_monitor_v2"
    except AttributeError:
        pass
    if shcore is not None:
        try:
            if shcore.SetProcessDpiAwareness(2) == 0:
                return "per_monitor"
        except OSError:
            pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except AttributeError:
        pass
    # Already set (e.g. by a manifest or by pywinauto) -> query.
    return current_dpi_awareness()


def current_dpi_awareness() -> str:
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        aw = user32.GetAwarenessFromDpiAwarenessContext(ctx)
        return {0: "unaware", 1: "system", 2: "per_monitor"}.get(aw, f"unknown({aw})")
    except AttributeError:
        return "unknown"


def window_dpi(hwnd: int) -> Optional[int]:
    try:
        return int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
    except AttributeError:
        return None


@dataclass
class MonitorInfo:
    device: str
    rect: Rect
    work: Rect
    primary: bool
    dpi: Optional[int]

    @property
    def scale(self) -> Optional[float]:
        return round(self.dpi / 96.0, 3) if self.dpi else None

    def to_dict(self) -> dict:
        return {"device": self.device, "rect": self.rect.to_dict(), "work": self.work.to_dict(),
                "primary": self.primary, "dpi": self.dpi, "scale": self.scale}


def monitors() -> list[MonitorInfo]:
    out: list[MonitorInfo] = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, _hdc, _rc, _lp):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        hmon = wintypes.HMONITOR(hmon)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        dpi = None
        if shcore is not None:
            dx, dy = wintypes.UINT(), wintypes.UINT()
            if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                dpi = int(dx.value)
        out.append(MonitorInfo(mi.szDevice, _rect(mi.rcMonitor), _rect(mi.rcWork), bool(mi.dwFlags & 1), dpi))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out


def virtual_screen() -> Rect:
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return Rect(x, y, x + w, y + h)


def monitors_for_rect(r: Rect) -> list[MonitorInfo]:
    return [m for m in monitors() if m.rect.intersects(r)]


# --------------------------------------------------------------------------- windows

@dataclass
class TopWindow:
    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: Rect
    owner: int
    exstyle: int


def get_window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def get_window_rect(hwnd: int) -> Rect:
    r = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return _rect(r)


def get_client_rect_screen(hwnd: int) -> Rect:
    r = wintypes.RECT()
    user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt))
    return Rect(pt.x, pt.y, pt.x + r.right, pt.y + r.bottom)


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(wintypes.HWND(hwnd)))


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(wintypes.HWND(hwnd)))


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(wintypes.HWND(hwnd)))


def is_cloaked(hwnd: int) -> bool:
    if dwmapi is None:
        return False
    val = wintypes.DWORD()
    res = dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return res == 0 and val.value != 0


def is_hung(hwnd: int) -> bool:
    return bool(user32.IsHungAppWindow(wintypes.HWND(hwnd)))


def get_exstyle(hwnd: int) -> int:
    return int(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)) & 0xFFFFFFFF


def get_owner(hwnd: int) -> int:
    return int(user32.GetWindow(wintypes.HWND(hwnd), GW_OWNER) or 0)


def root_owner(hwnd: int) -> int:
    return int(user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOTOWNER) or 0)


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def console_window() -> int:
    return int(kernel32.GetConsoleWindow() or 0)


def restore_window(hwnd: int) -> None:
    user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)


def top_level_windows(visible_only: bool = True) -> list[TopWindow]:
    """Top-level windows in z-order, topmost first (EnumWindows order)."""
    out: list[TopWindow] = []

    def cb(hwnd, _lp):
        h = int(hwnd)
        if visible_only and (not is_visible(h) or is_cloaked(h)):
            return True
        out.append(TopWindow(h, window_pid(h), get_window_text(h), get_class_name(h),
                             get_window_rect(h), get_owner(h), get_exstyle(h)))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def child_windows(parent: int, visible_only: bool = True) -> list[tuple[int, str, str, Rect]]:
    out = []

    def cb(hwnd, _lp):
        h = int(hwnd)
        if visible_only and not is_visible(h):
            return True
        out.append((h, get_class_name(h), get_window_text(h), get_window_rect(h)))
        return True

    user32.EnumChildWindows(wintypes.HWND(parent), WNDENUMPROC(cb), 0)
    return out


def process_image_name(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def display_affinity(hwnd: int) -> int:
    """0 = none; 1 = WDA_MONITOR; 0x11 = WDA_EXCLUDEFROMCAPTURE."""
    val = wintypes.DWORD()
    try:
        if user32.GetWindowDisplayAffinity(wintypes.HWND(hwnd), ctypes.byref(val)):
            return int(val.value)
    except AttributeError:
        pass
    return 0


def cursor_is_busy() -> bool:
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return False
    busy = {user32.LoadCursorW(None, ctypes.c_void_p(IDC_WAIT)),
            user32.LoadCursorW(None, ctypes.c_void_p(IDC_APPSTARTING))}
    return bool(ci.hCursor) and ci.hCursor in busy


def set_cursor_pos(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def post_message(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
    return bool(user32.PostMessageW(hwnd, msg, wparam, lparam))


def print_window(hwnd: int, hdc: int, flags: int) -> bool:
    return bool(user32.PrintWindow(hwnd, hdc, flags))


def message_box(text: str, title: str, flags: int) -> int:
    return int(user32.MessageBoxW(None, text, title, flags))


def beep() -> None:
    try:
        user32.MessageBeep(0x30)  # MB_ICONEXCLAMATION
    except Exception:
        pass


def os_version() -> str:
    import platform
    return f"{platform.platform()} ({platform.version()})"


def own_pid() -> int:
    return os.getpid()
