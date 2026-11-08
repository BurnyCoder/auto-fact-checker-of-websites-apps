"""UI logic tests (offscreen Qt): state -> overlay items, panel HTML escaping, monitor matching."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from factcheck.capture.layout import group_lines
from factcheck.capture.ocr import OcrLine
from factcheck.models import Box, Claim, SourceInfo, Verdict, VerdictSource
from factcheck.ui.panel import card_html
from factcheck.ui.state import UiState


@pytest.fixture(scope="module")
def qt():
    return QApplication.instance() or QApplication([])


def verdict(**kw):
    base = {"label": "Refuted", "confidence": 0.8, "confidence_reasoning": "r", "explanation": "x",
            "corrected_claim": "It is not visible.", "correction_confidence": 0.7, "correction_reasoning": "astronauts"}
    base.update(kw)
    return Verdict(**base)


def test_state_overlay_items_follow_layout_and_staleness(qt):
    state = UiState()
    src = SourceInfo(kind="desktop", source_id="win:1", app="discord.exe")
    blocks = group_lines([OcrLine("the Great Wall is visible from the Moon", Box(x=10, y=10, w=400, h=20,
                                                                                text="the Great Wall is visible from the Moon"))])
    claim = Claim(id="c1", text="The Great Wall is visible from the Moon.", span="Great Wall is visible from the Moon",
                  block_id=blocks[0].id, source=src, status="checking")
    state.on_event({"type": "layout", "source_id": "win:1", "blocks": blocks})
    state.on_event({"type": "claims", "source_id": "win:1", "claims": [claim]})
    items = state.overlay_items()
    assert len(items) == 1 and items[0].label is None and items[0].boxes[0].x > 10
    state.on_event({"type": "verdict", "source_id": "win:1", "claim_id": "c1", "verdict": verdict()})
    assert state.overlay_items()[0].label == "Refuted"
    state.on_event({"type": "frame_changed", "source_id": "win:1"})
    assert state.overlay_items() == []                                  # hidden while the window changes
    state.on_event({"type": "layout", "source_id": "win:1", "blocks": blocks})
    assert len(state.overlay_items()) == 1


def test_card_html_escapes_untrusted_text_and_links():
    src = SourceInfo(kind="desktop", source_id="w", app="<script>", title="t")
    claim = Claim(id="c", text="<img src=x onerror=alert(1)>", span="s", block_id="b", source=src)
    v = verdict(explanation="<b>bold</b>", sources=[VerdictSource(url="javascript:alert(1)", title="evil", supports="verdict"),
                                                     VerdictSource(url="https://ok.org/a?b=1&c=2", title="ok", quote="q")])
    out = card_html(claim, v)
    assert "<script>" not in out and "<img" not in out and "&lt;b&gt;bold" in out
    assert 'href="javascript' not in out and 'href="https://ok.org/a?b=1&amp;c=2"' in out
    assert "Correction (70%)" in out and "80%" in out


def test_overlay_paints_box_at_converted_position(qt):
    """Offscreen render: a Refuted claim at physical (110..410, 210..230) on a monitor whose origin is (100, 200)
    must be painted red at local (10.., 10..) when devicePixelRatio is 1."""
    from PySide6.QtGui import QGuiApplication

    from factcheck.capture import winapi
    from factcheck.ui.overlay import ScreenOverlay

    screen = QGuiApplication.primaryScreen()
    geo = screen.geometry()
    mon = winapi.Monitor(device="test", rect=winapi.Rect(100, 200, geo.width(), geo.height()), primary=True)
    state = UiState()
    text = "the Great Wall is visible from the Moon"
    blocks = group_lines([OcrLine(text, Box(x=110, y=210, w=300, h=20, text=text))])
    src = SourceInfo(kind="desktop", source_id="win:1", app="x")
    state.on_event({"type": "layout", "source_id": "win:1", "blocks": blocks})
    state.on_event({"type": "claims", "source_id": "win:1", "claims": [
        Claim(id="c1", text=text, span=text, block_id=blocks[0].id, source=src)]})
    state.on_event({"type": "verdict", "source_id": "win:1", "claim_id": "c1", "verdict": verdict(confidence=1.0)})
    overlay = ScreenOverlay(screen, mon, state, debug_visible_in_capture=True)
    image = overlay.grab().toImage()
    dpr = screen.devicePixelRatio()
    edge = image.pixelColor(int(8 * dpr), int(20 * dpr))          # left border of the box (x = 10 - 2 px margin)
    assert edge.red() > 180 and edge.green() < 120, edge.name()  # Refuted red
    inside_far = image.pixelColor(int(600 * dpr), int(400 * dpr))
    assert inside_far.alpha() == 0                                 # rest of the overlay stays transparent
    overlay.close()
