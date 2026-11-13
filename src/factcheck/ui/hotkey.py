"""Global hotkeys via Win32 RegisterHotKey, delivered through a Qt native event filter.

Local context: register each hotkey on the Qt main thread (RegisterHotKey posts WM_HOTKEY to the
registering thread's message queue, with the hotkey's id in wParam) and call its callback when it arrives.
Bindings: Ctrl+Alt+F checks every monitor, Ctrl+Alt+R opens the region picker (both configurable in .env).
Global context: chosen over low-level keyboard hooks (pynput/keyboard), which Windows silently removes
if they respond slowly. QAbstractNativeEventFilter:
https://doc.qt.io/qtforpython-6/PySide6/QtCore/QAbstractNativeEventFilter.html
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

from factcheck.capture import winapi

log = logging.getLogger(__name__)
HOTKEY_ID = 0xFC01


class HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, bindings: list[tuple[str, Callable[[], None]]]):
        """`bindings`: (spec such as "ctrl+alt+f", callback). Binding i gets the id HOTKEY_ID + i."""
        super().__init__()
        self.callbacks: dict[int, Callable[[], None]] = {}
        for i, (spec, callback) in enumerate(bindings):
            if winapi.register_hotkey(HOTKEY_ID + i, spec):
                self.callbacks[HOTKEY_ID + i] = callback
                log.info("global hotkey %s registered", spec)
            else:
                log.warning("could not register hotkey %s (already used by another app?)", spec)
        QCoreApplication.instance().installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == winapi.WM_HOTKEY and msg.wParam in self.callbacks:
                self.callbacks[msg.wParam]()
                return True, 0
        return False, 0

    def close(self) -> None:
        for hotkey_id in self.callbacks:
            winapi.unregister_hotkey(hotkey_id)
        self.callbacks = {}
        QCoreApplication.instance().removeNativeEventFilter(self)
