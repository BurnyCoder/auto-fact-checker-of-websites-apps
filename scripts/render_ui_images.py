"""Render the README's UI images from the real widgets with fixed sample data ($0, no model calls).

Usage:  uv run python scripts/render_ui_images.py            # all three images into docs/images/
Outputs:
  panel-dark-mode.png / panel-light-mode.png  the side panel (one unchecked card, one verdict card) in the real
                                              Windows 11 style, with dark / light requested for this app only
                                              (QStyleHints.setColorScheme, Qt 6.8+), so the system theme does not matter
  region-picker.png                           the Ctrl+Alt+R picker with a selection over a sample chat, rendered on
                                              Qt's offscreen platform (no dimmed window appears on the real screen)
Each image is rendered in its own subprocess: the Qt platform (windows / offscreen) is fixed per process.
The sample data is illustrative; the verdicts are not model outputs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"


def render_panel(scheme: str, path: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from factcheck.models import Claim, SourceInfo, Verdict, VerdictSource
    from factcheck.ui.panel import Panel
    from factcheck.ui.state import UiState

    app = QApplication([])
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark if scheme == "dark" else Qt.ColorScheme.Light)
    state = UiState()
    screen = SourceInfo(kind="desktop", source_id="screen:1", app="screen", title=r"\\.\DISPLAY1")
    web = SourceInfo(kind="chrome", source_id="tab:1", app="chrome", title="Joe Cahill - Wikipedia")
    pending = Claim(id="p", text="LLMs cannot yet do the most creative out-of-distribution science.", span="s",
                    block_id="b", source=screen, status="pending")
    checked = Claim(id="c", text="Joe Cahill died in July 2005 at the age of 84.", span="s", block_id="b", source=web)
    verdict = Verdict(
        label="Refuted", confidence=0.85,
        confidence_reasoning="Two independent sources agree on a July 2004 death at age 84.",
        explanation="Contemporary obituaries give 2004, not 2005.",
        corrected_claim="Joe Cahill died in July 2004 at the age of 84.", correction_confidence=0.85,
        correction_reasoning="Same sources give the year 2004.",
        sources=[VerdictSource(url="https://en.wikipedia.org/wiki/Joe_Cahill", title="Joe Cahill - Wikipedia",
                               supports="verdict", quote_verified=True, quote="died on 23 July 2004"),
                 VerdictSource(url="https://www.irishtimes.com/x", title="Irish Times obituary", supports="correction",
                               quote_verified=False)],
        flags=["quote-unverified"], cost_usd=0.0155)
    state.on_event({"type": "claims", "source_id": "tab:1", "claims": [checked]})
    state.on_event({"type": "verdict", "source_id": "tab:1", "claim_id": "c", "verdict": verdict})
    state.on_event({"type": "claims", "source_id": "screen:1", "claims": [pending]})
    panel = Panel(state, on_check=lambda _c: None, status_text=lambda: "Spent $0.42 of $20.00 · trigger: hotkey",
                  auto_checks=3)
    panel.resize(460, 620)
    panel.show()
    for _ in range(10):
        app.processEvents()
    panel.grab().save(str(path))


def render_picker(path: Path) -> None:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    from factcheck.capture import winapi
    from factcheck.ui.region import RegionPicker

    app = QApplication([])
    screen = app.primaryScreen()
    g = screen.geometry()
    picker = RegionPicker(screen, winapi.Monitor("T", winapi.Rect(0, 0, g.width(), g.height()), True), lambda _r: None)
    picker.start, picker.end = QPoint(120, 180), QPoint(620, 330)
    layer = picker.grab()
    image = QPixmap(layer.size())
    image.fill(QColor("#313338"))                                # a generic dark chat window behind the picker
    p = QPainter(image)
    p.setPen(QColor("#dbdee1"))
    p.setFont(QFont("Segoe UI", 12))
    for i, line in enumerate(["sarah  Today at 14:03", "Coffee is the second most traded commodity after oil.",
                              "mike_92  Today at 14:05", "The Eiffel Tower is 330 metres tall and opened in 1889.",
                              "Paris has about 2.1 million residents."]):
        p.drawText(140, 200 + i * 28, line)
    p.drawPixmap(0, 0, layer)
    p.end()
    image.save(str(path))


def main() -> None:
    if len(sys.argv) == 3:                                       # child process: one image
        what, path = sys.argv[1], Path(sys.argv[2])
        render_picker(path) if what == "picker" else render_panel(what, path)
        print("saved", path)
        return
    jobs = [("dark", OUT / "panel-dark-mode.png", {}), ("light", OUT / "panel-light-mode.png", {}),
            ("picker", OUT / "region-picker.png", {"QT_QPA_PLATFORM": "offscreen", "QT_QPA_FONTDIR": "C:/Windows/Fonts"})]
    for what, path, env in jobs:
        subprocess.run([sys.executable, __file__, what, str(path)], check=True, env={**os.environ, **env})


if __name__ == "__main__":
    main()
