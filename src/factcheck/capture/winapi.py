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
RegisterHotKey (https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
DWMWA_EXTENDED_FRAME_BOUNDS = 9
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
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process = psutil.Process(pid.value).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process = ""
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))            # fallback
    return WindowInfo(hwnd=int(hwnd), title=buf.value, pid=pid.value, process=process,
                      rect=Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                      minimized=bool(user32.IsIconic(hwnd)))


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
