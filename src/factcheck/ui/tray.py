"""System tray icon and menu: pause, check now, modes, panel, extension token, logs, budget, quit.

Local context: builds the tray menu and draws a simple icon (no image assets needed).
Global context: the tray is the app's main control surface; actions call back into app.py.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def make_icon(paused: bool = False) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#80868b" if paused else "#1a73e8"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QPen(QColor("white"), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPolyline([QPointF(18, 33), QPointF(28, 43), QPointF(46, 22)])   # check mark
    p.end()
    return QIcon(pix)


class Tray(QSystemTrayIcon):
    def __init__(self, *, mode: str, display: str, on_pause: Callable[[bool], None], on_check_now: Callable[[], None],
                 on_check_part: Callable[[], None], on_mode: Callable[[str], None], on_display: Callable[[str], None], on_show_panel: Callable[[], None],
                 on_copy_token: Callable[[], None], on_open_logs: Callable[[], None], budget_text: Callable[[], str],
                 on_quit: Callable[[], None]):
        super().__init__(make_icon())
        self.setToolTip("Fact check what you see")
        self.budget_text = budget_text
        menu = QMenu()
        self.pause_action = QAction("Pause capture", menu, checkable=True)
        self.pause_action.toggled.connect(lambda on: (on_pause(on), self.setIcon(make_icon(on))))
        menu.addAction(self.pause_action)
        menu.addAction("Check screen now", on_check_now)
        menu.addAction("Check part of screen…", on_check_part)
        menu.addAction("Show panel", on_show_panel)
        menu.addSeparator()
        for title, options, current, handler in (("Trigger", ("continuous", "hotkey", "both"), mode, on_mode),
                                                 ("Display", ("overlay", "panel", "both"), display, on_display)):
            sub = menu.addMenu(title)
            group = QActionGroup(sub)
            for opt in options:
                act = QAction(opt, sub, checkable=True, checked=(opt == current))
                act.triggered.connect(lambda _c=False, o=opt, h=handler: h(o))
                group.addAction(act)
                sub.addAction(act)
        menu.addSeparator()
        menu.addAction("Copy extension token", on_copy_token)
        menu.addAction("Open logs folder", on_open_logs)
        self.budget_action = QAction("", menu, enabled=False)
        menu.addAction(self.budget_action)
        menu.aboutToShow.connect(lambda: self.budget_action.setText(self.budget_text()))
        menu.addSeparator()
        menu.addAction("Quit", on_quit)
        self.setContextMenu(menu)
        self.activated.connect(lambda reason: on_show_panel() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.show()
