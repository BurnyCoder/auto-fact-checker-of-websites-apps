"""Small ctypes wrappers over Win32 APIs used by capture and the overlay.

Local context: foreground-window info, monitor list (device names + physical rectangles), the
capture-exclusion flag for overlay windows, and global hotkeys.
Global context: the process is per-monitor-DPI-aware (Qt6 sets this on Windows), so every rectangle
here is in physical pixels, the same space mss screenshots and OCR boxes use.
API references: GetForegroundWindow / GetWindowTextW / GetWindowThreadProcessId / IsIconic
(https://learn.microsoft.com/en-us/windows/win32/api/winuser/), DwmGetWindowAttribute with
DWMWA_EXTENDED_FRAME_BOUNDS (the visible frame, without the invisible resize borders GetWindowRect
includes: https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute),
EnumDisplayMonitors / GetMonitorInfoW, SetWindowDisplayAffinity with WDA_EXCLUDEFROMCAPTURE (Windows 10
2004+: https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity),
RegisterHotKey (https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey),
GetTopWindow + GetWindow(GW_HWNDNEXT) to walk top-level windows in z-order
(https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindow), DWMWA_CLOAKED (windows on other
virtual desktops or suspended UWP apps report IsWindowVisible but are not drawn) and WS_EX_TRANSPARENT (click-through
overlays such as ours: Qt::WindowTransparentForInput sets it).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
GWL_EXSTYLE, GW_HWNDNEXT = -20, 2
WS_EX_TRANSPARENT, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x20, 0x80000, 0x8000000
user32.GetTopWindow.restype = user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process: str          # lowercase executable name, e.g. "discord.exe"
    rect: Rect
    minimized: bool
    see_through: bool = False   # layered and never activated (e.g. the GeForce overlay): may be transparent

    @property
    def is_own(self) -> bool:
        return self.pid == os.getpid()


@dataclass(frozen=True)
class Monitor:
    device: str           # e.g. \\.\DISPLAY1 (matches QScreen.name() on Windows)
    rect: Rect            # physical pixels on the virtual desktop
    primary: bool


def foreground_window() -> WindowInfo | None:
    hwnd = user32.GetForegroundWindow()
    return window_info(hwnd) if hwnd else None


def window_info(hwnd: int, names: dict[int, str] | None = None, see_through: bool = False) -> WindowInfo:
    """Title, process and visible frame of one top-level window (`names` caches pid -> process name)."""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    names = {} if names is None else names
    if pid.value not in names:
        try:
            names[pid.value] = psutil.Process(pid.value).name().lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            names[pid.value] = ""
    process = names[pid.value]
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))            # fallback
    return WindowInfo(hwnd=int(hwnd), title=buf.value, pid=pid.value, process=process,
                      rect=Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                      minimized=bool(user32.IsIconic(hwnd)), see_through=see_through)


def visible_windows() -> list[WindowInfo]:
    """Top-level windows that are drawn on screen, topmost first (z-order). Skips hidden, minimized, cloaked and
    click-through windows (overlays, ours included: they cover the screen but show almost nothing). Layered windows
    that never take focus are kept but marked `see_through`: they may be transparent, so they must not count as
    covering what is below them (measured 2026-11-13: the GeForce overlay is layered + no-activate; Electron apps
    such as Claude and Discord's are layered but activate)."""
    found, names = [], {}
    hwnd = user32.GetTopWindow(None)
    for _ in range(10000):                         # bounded: the z-order can change while we walk it (GetWindow docs)
        if not hwnd:
            break
        cloaked = wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not cloaked.value and not ex & WS_EX_TRANSPARENT:
            overlay_like = (ex & (WS_EX_LAYERED | WS_EX_NOACTIVATE)) == (WS_EX_LAYERED | WS_EX_NOACTIVATE)
            info = window_info(hwnd, names, see_through=overlay_like)
            if info.rect.width > 0 and info.rect.height > 0:
                found.append(info)
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
    return found


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def monitors() -> list[Monitor]:
    found: list[Monitor] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmon, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(device=info.szDevice, rect=Rect(r.left, r.top, r.right - r.left, r.bottom - r.top),
                                 primary=bool(info.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(None, None, proc(callback), 0)
    return found


def exclude_from_capture(hwnd: int, exclude: bool = True) -> bool:
    """Hide a window from screenshots (our overlay must not OCR its own boxes). Returns success."""
    return bool(user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE))


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+f' -> (MOD_CONTROL|MOD_ALT|MOD_NOREPEAT, VK_F). Letters, digits and F1-F12 are supported."""
    mods, key = MOD_NOREPEAT, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super"):
            mods |= MOD_WIN
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())                              # VK codes for A-Z / 0-9 equal ASCII
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 12:
            key = 0x70 + int(part[1:]) - 1                       # VK_F1 = 0x70
    if key is None:
        raise ValueError(f"unsupported hotkey: {spec!r}")
    return mods, key


def register_hotkey(hotkey_id: int, spec: str) -> bool:
    """Register a system-wide hotkey for the calling thread's message queue (must be the Qt main thread)."""
    mods, vk = parse_hotkey(spec)
    return bool(user32.RegisterHotKey(None, hotkey_id, mods, vk))


def unregister_hotkey(hotkey_id: int) -> None:
    user32.UnregisterHotKey(None, hotkey_id)
