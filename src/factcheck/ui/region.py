"""Region picker: drag a box over the part of the screen to fact-check (Ctrl+Alt+R or the tray), like Snipping Tool.

Local context: `RegionPickerManager.pick()` covers every monitor with a dimmed, frameless, top-most window
(`RegionPicker`); the user drags a rectangle on one of them, Esc / right-click / a plain click cancels. The selection
is converted from the window's logical coordinates to physical desktop pixels with `to_physical`, the inverse of the
overlay's `local = (physical - monitor origin) / devicePixelRatio`, so it is right on mixed-DPI setups.
Global context: the physical rectangle goes to `DesktopCapture.capture_region` (capture/watcher.py), which OCRs only
that rectangle. The pickers are closed BEFORE the callback runs: the capture lists visible windows and would treat a
still-visible picker as one of our own windows and black the monitor out. They are also excluded from screen capture
(WDA_EXCLUDEFROMCAPTURE), so their dim layer can never end up in a screenshot.
Qt references: mouse/key events https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QWidget.html ,
QPainter.CompositionMode_Clear https://doc.qt.io/qtforpython-6/PySide6/QtGui/QPainter.html#PySide6.QtGui.QPainter.CompositionMode
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen, QScreen
from PySide6.QtWidgets import QWidget

from factcheck.capture import winapi
from factcheck.ui.overlay import match_monitor

log = logging.getLogger(__name__)
MIN_SIDE_PX = 8                       # physical pixels: anything smaller is a click, which cancels
HINT = "Drag over the part to fact-check  ·  Esc to cancel"


def to_physical(start, end, monitor: winapi.Monitor, dpr: float) -> winapi.Rect | None:
    """Two local (logical) points of a drag on `monitor` -> the physical desktop rectangle, any drag direction.
    None when the box is under MIN_SIDE_PX on either side."""
    x0, x1 = sorted((start.x(), end.x()))
    y0, y1 = sorted((start.y(), end.y()))
    left, top = monitor.rect.left + round(x0 * dpr), monitor.rect.top + round(y0 * dpr)
    width, height = round((x1 - x0) * dpr), round((y1 - y0) * dpr)
    if width < MIN_SIDE_PX or height < MIN_SIDE_PX:
        return None
    return winapi.Rect(left, top, width, height)


class RegionPicker(QWidget):
    """One monitor's dimmed selection layer. Reports a finished drag or a cancel to its manager."""

    def __init__(self, screen: QScreen, monitor: winapi.Monitor, finish: Callable[[winapi.Rect | None], None]):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.screen_, self.monitor, self.finish = screen, monitor, finish
        self.start: QPoint | None = None
        self.end: QPoint | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.show()
        winapi.exclude_from_capture(int(self.winId()))      # never in a screenshot (fails harmlessly offscreen)

    def selection(self) -> QRect | None:
        return QRect(self.start, self.end).normalized() if self.start is not None and self.end is not None else None

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))                   # dim everything (43 %)...
        sel = self.selection()
        if sel is not None:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(sel, Qt.GlobalColor.transparent)                 # ...except the selection
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor("#1a73e8"), 2))
            p.drawRect(sel)
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        hint = p.fontMetrics().boundingRect(HINT).adjusted(-12, -6, 12, 6)
        hint.moveCenter(QPoint(self.width() // 2, 32))
        p.fillRect(hint, QColor(32, 33, 36, 230))
        p.setPen(QColor("white"))
        p.drawText(hint, Qt.AlignmentFlag.AlignCenter, HINT)
        p.end()

    # Drag state comes from press/release, not event.buttons(), so synthetic moves (tests, touchpads) work too.
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            self.finish(None)                                           # right-click cancels, as in Snipping Tool
            return
        self.start = self.end = event.position().toPoint()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.start is not None:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self.start is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self.end = event.position().toPoint()
        self.finish(to_physical(self.start, self.end, self.monitor, self.screen_.devicePixelRatio()))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.finish(None)


class RegionPickerManager:
    """Opens one picker per monitor; the first finished drag (or a cancel) closes them all, then calls back."""

    def __init__(self, monitors: Callable[[], list[winapi.Monitor]] = winapi.monitors):
        self.monitors = monitors
        self.pickers: list[RegionPicker] = []
        self._on_done: Callable[[winapi.Rect], None] | None = None
        self._on_cancel: Callable[[], None] | None = None

    def pick(self, on_done: Callable[[winapi.Rect], None], on_cancel: Callable[[], None] = lambda: None) -> None:
        self.close()                                                    # a second press restarts the picker
        self._on_done, self._on_cancel = on_done, on_cancel
        mons = self.monitors()
        for screen in QGuiApplication.screens():
            mon = match_monitor(screen, mons)
            if mon is None:
                log.warning("region picker: no physical monitor matched screen %s", screen.name())
                continue
            self.pickers.append(RegionPicker(screen, mon, self._finish))
        under_cursor = QGuiApplication.screenAt(QCursor.pos())
        for picker in self.pickers:                                     # keyboard focus (Esc) on the cursor's screen
            if picker.screen_ is under_cursor or len(self.pickers) == 1:
                picker.raise_()
                picker.activateWindow()

    def close(self) -> None:
        for picker in self.pickers:
            picker.close()
            picker.deleteLater()
        self.pickers = []

    def _finish(self, rect: winapi.Rect | None) -> None:
        on_done, on_cancel = self._on_done, self._on_cancel
        self._on_done = self._on_cancel = None
        self.close()                                                    # hidden before any screenshot is taken
        if rect is None:
            log.info("region picker cancelled")
            if on_cancel:
                on_cancel()
        elif on_done:
            log.info("region picked: %s", rect)
            on_done(rect)
