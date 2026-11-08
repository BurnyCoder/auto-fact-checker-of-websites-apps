"""Global hotkey via Win32 RegisterHotKey, delivered through a Qt native event filter.

Local context: register the hotkey on the Qt main thread (RegisterHotKey posts WM_HOTKEY to the
registering thread's message queue) and call `callback` when WM_HOTKEY arrives.
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
    def __init__(self, spec: str, callback: Callable[[], None]):
        super().__init__()
        self.callback = callback
        self.ok = winapi.register_hotkey(HOTKEY_ID, spec)
        if self.ok:
            QCoreApplication.instance().installNativeEventFilter(self)
            log.info("global hotkey %s registered", spec)
        else:
            log.warning("could not register hotkey %s (already used by another app?)", spec)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == winapi.WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.callback()
                return True, 0
        return False, 0

    def close(self) -> None:
        if self.ok:
            winapi.unregister_hotkey(HOTKEY_ID)
