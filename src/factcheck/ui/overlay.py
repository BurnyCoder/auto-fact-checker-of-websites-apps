"""Click-through, always-on-top overlay windows (one per monitor) that box claims on screen.

Local context: each `ScreenOverlay` covers one QScreen; boxes arrive in physical virtual-desktop pixels
and are converted with local = (physical - monitor_origin) / devicePixelRatio, which stays correct on
mixed-DPI multi-monitor setups (one window spanning monitors would not, because Qt's global logical
coordinates are not linear across differently scaled screens).
Global context: windows are excluded from screen capture (WDA_EXCLUDEFROMCAPTURE) so the OCR never reads
our own boxes; `debug_visible_in_capture=True` keeps them visible to screenshots for debugging/docs.
Qt flags: https://doc.qt.io/qtforpython-6/PySide6/QtCore/Qt.html#PySide6.QtCore.Qt.WindowType
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QScreen
from PySide6.QtWidgets import QWidget

from factcheck.capture import winapi
from factcheck.models import LABEL_COLOURS
from factcheck.ui.state import UiState

log = logging.getLogger(__name__)
SHORT = {"Supported": "Supported", "Refuted": "Refuted", "Not Enough Evidence": "Not enough evidence",
         "Conflicting Evidence/Cherrypicking": "Cherry-picked"}


def place_tag(first: QRectF, last: QRectF, width: float, screen_width: float, occupied: list[QRectF]) -> QRectF:
    """Where to draw a claim's verdict tag: right of its last box if that spot is on screen and covers no other claim
    or tag (keeps the text above readable), otherwise just above its first box. Before this check, with two claims on
    one line the first claim's tag covered the second claim's text (README overlay demo, 2026-11-13)."""
    right = QRectF(last.right() + 6, last.center().y() - 7, width, 14)
    if right.right() < screen_width and not any(right.intersects(r) for r in occupied):
        return right
    return QRectF(first.left(), max(0.0, first.top() - 15), width, 14)


def match_monitor(screen: QScreen, mons: list[winapi.Monitor]) -> winapi.Monitor | None:
    """Pair a QScreen with its physical monitor: by device name first, else by physical size + position order."""
    for m in mons:
        if m.device == screen.name():
            return m
    dpr = screen.devicePixelRatio()
    geo = screen.geometry()
    same_size = [m for m in mons if abs(m.rect.width - geo.width() * dpr) <= 2 and abs(m.rect.height - geo.height() * dpr) <= 2]
    same_size.sort(key=lambda m: (abs(m.rect.left - geo.x() * dpr), abs(m.rect.top - geo.y() * dpr)))
    return same_size[0] if same_size else None


class ScreenOverlay(QWidget):
    def __init__(self, screen: QScreen, monitor: winapi.Monitor, state: UiState, debug_visible_in_capture: bool):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.screen_ = screen
        self.monitor = monitor
        self.state = state
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.show()
        if not debug_visible_in_capture and not winapi.exclude_from_capture(int(self.winId())):
            log.warning("could not exclude overlay on %s from capture; OCR may read the boxes", monitor.device)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dpr = self.screen_.devicePixelRatio()
        mx, my, mw, mh = self.monitor.rect.left, self.monitor.rect.top, self.monitor.rect.width, self.monitor.rect.height
        font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        painter.setFont(font)
        # local rectangles of every claim on this monitor first, so a tag can avoid all of them
        laid_out = []
        for item in self.state.overlay_items():
            rects = [QRectF((b.x - mx) / dpr - 2, (b.y - my) / dpr - 1, b.w / dpr + 4, b.h / dpr + 2)
                     for b in item.boxes if mx <= b.x < mx + mw and my <= b.y < my + mh]   # skip other monitors' boxes
            if rects:
                laid_out.append((item, rects))
        occupied = [r for _, rects in laid_out for r in rects]
        for item, rects in laid_out:
            colour = QColor(LABEL_COLOURS.get(item.label, "#1a73e8"))
            width = 1.5 + 3.0 * (item.confidence or 0.0)        # border strength scales with confidence
            pen = QPen(colour, width)
            if item.label is None:
                pen.setStyle(Qt.PenStyle.DotLine)               # still checking / not checked yet
            painter.setPen(pen)
            fill = QColor(colour)
            fill.setAlpha(28 if item.label in ("Refuted", "Conflicting Evidence/Cherrypicking") else 0)
            painter.setBrush(fill)
            for rect in rects:
                painter.drawRoundedRect(rect, 3, 3)
            tag = SHORT.get(item.label, "Checking…" if item.status == "checking" else "Not checked")
            if item.confidence is not None:
                tag += f" {round(item.confidence * 100)}%"
            tw = painter.fontMetrics().horizontalAdvance(tag) + 8
            tag_rect = place_tag(rects[0], rects[-1], tw, self.width(), occupied)
            occupied.append(tag_rect)
            painter.fillRect(tag_rect, colour)
            painter.setPen(QColor("white"))
            painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag)
        painter.end()


class OverlayManager:
    """Creates one overlay per screen and repaints them when the UI state changes."""

    def __init__(self, state: UiState, debug_visible_in_capture: bool = False):
        self.state = state
        self.debug = debug_visible_in_capture
        self.overlays: list[ScreenOverlay] = []
        self.rebuild()
        state.changed.connect(self.refresh)
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda _s: self.rebuild())
        app.screenRemoved.connect(lambda _s: self.rebuild())

    def rebuild(self) -> None:
        for o in self.overlays:
            o.close()
        mons = winapi.monitors()
        self.overlays = []
        for screen in QGuiApplication.screens():
            mon = match_monitor(screen, mons)
            if mon is None:
                log.warning("no physical monitor matched screen %s", screen.name())
                continue
            self.overlays.append(ScreenOverlay(screen, mon, self.state, self.debug))
        log.info("overlay on %d screen(s): %s", len(self.overlays), [o.monitor.device for o in self.overlays])

    def refresh(self) -> None:
        for o in self.overlays:
            o.update()

    def set_visible(self, visible: bool) -> None:
        for o in self.overlays:
            o.setVisible(visible)
