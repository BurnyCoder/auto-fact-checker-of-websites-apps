"""UI logic tests (offscreen Qt): state -> overlay items, panel HTML escaping, monitor matching."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")   # the offscreen platform has no font database otherwise
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


def test_panel_card_is_readable_in_dark_mode(qt):
    """Regression (2026-11-13, user screenshot): with Windows in dark mode the system palette's white text was drawn
    on the card's white background, and links were pale cyan."""
    from PySide6.QtGui import QColor, QPalette

    from factcheck.ui.panel import ClaimCard

    old = qt.palette()
    dark = QPalette(old)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
        dark.setColor(role, QColor("white"))
    dark.setColor(QPalette.ColorRole.Link, QColor("#9be7ff"))
    qt.setPalette(dark)
    try:
        card = ClaimCard(lambda _cid: None)
        src = SourceInfo(kind="chrome", source_id="t", app="chrome", title="Joe Cahill - Wikipedia")
        claim = Claim(id="c", text="Joe Cahill died in 2004.", span="s", block_id="b", source=src)
        card.update_from(claim, verdict(label="Supported", corrected_claim=None,
                                        sources=[VerdictSource(url="https://a.org", title="A", supports="verdict")]))
        card.ensurePolished()
        card.label.ensurePolished()
        text = card.label.palette().color(QPalette.ColorRole.WindowText)
        assert text.lightness() < 80                       # dark text on the white card
        assert "color:#1a73e8" in card.label.text()        # links get a fixed, readable blue
    finally:
        qt.setPalette(old)


def test_check_button_is_visible_in_dark_mode(qt):
    """Regression (2026-11-13, user: "I don't see the button"): Windows 11 dark mode drew the Check button with white
    text on the white card. Simulate it with a white-on-white palette; the button must still show dark pixels."""
    from PySide6.QtGui import QColor, QPalette

    from factcheck.ui.panel import ClaimCard

    old = qt.palette()
    white = QPalette(old)
    for role in (QPalette.ColorRole.Button, QPalette.ColorRole.ButtonText, QPalette.ColorRole.Window):
        white.setColor(role, QColor("white"))
    qt.setPalette(white)
    try:
        card = ClaimCard(lambda _cid: None, auto_checks=3)
        src = SourceInfo(kind="desktop", source_id="screen:x", app="screen", title="DISPLAY1")
        card.update_from(Claim(id="c", text="x", span="s", block_id="b", source=src, status="pending"), None)
        card.resize(440, 160)
        card.show()
        qt.processEvents()
        assert card.button.isVisible() and card.button.text() == "Check this claim"
        image, r = card.grab().toImage(), card.button.geometry()
        dpr = image.devicePixelRatio()
        darkest = min(image.pixelColor(int(x * dpr), int(y * dpr)).lightness()
                      for x in range(r.left(), r.right(), 2) for y in range(r.top(), r.bottom(), 2))
        assert darkest < 160                                  # text/border drawn in a colour that shows on white
        card.close()
    finally:
        qt.setPalette(old)


def test_pending_card_says_how_to_check_it():
    src = SourceInfo(kind="desktop", source_id="screen:x", app="screen", title="DISPLAY1")
    out = card_html(Claim(id="c", text="Claim.", span="s", block_id="b", source=src, status="pending"), None,
                    auto_checks=3)
    assert "first 3" in out and "Check this claim" in out
    assert "</div><div" in out.split("Claim.")[0]             # claim text on its own line, not run into the source


def test_screen_boxes_stay_for_the_ttl_after_the_last_verdict(qt, monkeypatch):
    """Regression (2026-11-13 live check): hotkey boxes expired 60 s after the capture, while verdicts took 22-60 s,
    so they vanished right after (or before) the results came in. The clock now restarts on every result."""
    import factcheck.ui.state as state_mod

    now = [1000.0]
    monkeypatch.setattr(state_mod.time, "monotonic", lambda: now[0])
    state = UiState()
    text = "the Great Wall is visible from the Moon"
    blocks = group_lines([OcrLine(text, Box(x=10, y=10, w=400, h=20, text=text))])
    src = SourceInfo(kind="desktop", source_id="screen:D1", app="screen")
    state.on_event({"type": "layout", "source_id": "screen:D1", "blocks": blocks})
    state.on_event({"type": "claims", "source_id": "screen:D1", "claims": [
        Claim(id="c1", text=text, span=text, block_id=blocks[0].id, source=src, status="checking")]})
    now[0] += 55
    state.on_event({"type": "verdict", "source_id": "screen:D1", "claim_id": "c1", "verdict": verdict()})
    now[0] += state_mod.SCREEN_LAYOUT_TTL_S - 1
    state.expire()
    assert [i.label for i in state.overlay_items()] == ["Refuted"]   # still readable 59 s after the verdict
    changed = []
    state.changed.connect(lambda: changed.append(1))
    now[0] += 2
    state.expire()                                             # the 1 s timer: boxes go even with no new event
    assert state.overlay_items() == [] and changed == [1]


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio (https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio, relative luminance per
    https://www.w3.org/TR/WCAG21/#dfn-relative-luminance)."""
    def lum(hex_colour: str) -> float:
        rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_card_themes_have_readable_contrast():
    """Both themes meet WCAG AA (4.5:1) for every text colour on the backgrounds it is drawn on."""
    from factcheck.ui.panel import DARK, LIGHT

    for t in (LIGHT, DARK):
        for fg in (t.text, t.muted, t.link):
            assert contrast(fg, t.card) >= 4.5, (t, fg)
        for fg in (t.text, t.muted):
            assert contrast(fg, t.corr) >= 4.5, (t, fg)
    assert contrast(DARK.card, "#000000") < 3 and contrast(LIGHT.card, "#ffffff") < 1.1   # dark card, light card


def test_dark_scheme_gives_dark_cards(qt):
    """User (2026-11-13): no white background in dark mode. The panel picks the theme from the system colour scheme
    and re-themes existing cards when it changes."""
    from PySide6.QtCore import Qt

    from factcheck.ui.panel import DARK, LIGHT, Panel, theme_for

    assert theme_for(Qt.ColorScheme.Dark) is DARK
    assert theme_for(Qt.ColorScheme.Light) is LIGHT and theme_for(Qt.ColorScheme.Unknown) is LIGHT
    state = UiState()
    src = SourceInfo(kind="desktop", source_id="screen:x", app="screen", title="DISPLAY1")
    state.on_event({"type": "claims", "source_id": "screen:x", "claims": [
        Claim(id="c", text="Claim.", span="s", block_id="b", source=src, status="pending")]})
    panel = Panel(state, on_check=lambda _c: None, status_text=lambda: "")
    panel.set_theme(DARK)
    card = panel.cards["c"]
    assert DARK.muted in card.label.text() and LIGHT.muted not in card.label.text()
    card.resize(440, 160)
    card.show()
    qt.processEvents()
    image = card.grab().toImage()
    dpr = image.devicePixelRatio()
    assert image.pixelColor(int(card.width() * dpr) - 8, int(4 * dpr)).lightness() < 80     # dark background
    r = card.button.geometry()
    assert max(image.pixelColor(int(x * dpr), int(y * dpr)).lightness()
               for x in range(r.left(), r.right(), 2) for y in range(r.top(), r.bottom(), 2)) > 150   # button shows
    panel.set_theme(LIGHT)
    assert LIGHT.muted in card.label.text()
    panel.close()


def test_to_physical_maps_a_drag_to_monitor_pixels():
    """Local (logical) picker coordinates -> physical desktop pixels: the inverse of the overlay's mapping."""
    from PySide6.QtCore import QPoint

    from factcheck.capture import winapi
    from factcheck.ui.region import to_physical

    mon = winapi.Monitor(device="D", rect=winapi.Rect(-2560, -159, 2560, 1440), primary=False)
    assert to_physical(QPoint(200, 100), QPoint(40, 20), mon, 1.5) == winapi.Rect(-2560 + 60, -159 + 30, 240, 120)
    assert to_physical(QPoint(10, 10), QPoint(13, 12), mon, 1.0) is None          # a click, not a box


def test_region_picker_returns_the_dragged_rect_after_closing(qt):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest

    from factcheck.capture import winapi
    from factcheck.ui.region import RegionPickerManager

    screen = QGuiApplication.primaryScreen()
    geo, dpr = screen.geometry(), screen.devicePixelRatio()
    mon = winapi.Monitor(device="T", rect=winapi.Rect(100, 200, round(geo.width() * dpr), round(geo.height() * dpr)),
                         primary=True)
    manager = RegionPickerManager(monitors=lambda: [mon])
    done, cancelled = [], []
    manager.pick(on_done=lambda r: done.append((r, picker.isVisible())), on_cancel=lambda: cancelled.append(1))
    picker = manager.pickers[0]
    assert picker.isVisible()
    QTest.mousePress(picker, Qt.MouseButton.LeftButton, pos=QPoint(10, 20))
    QTest.mouseMove(picker, QPoint(110, 70))
    QTest.mouseRelease(picker, Qt.MouseButton.LeftButton, pos=QPoint(110, 70))
    rect, visible_at_callback = done[0]
    assert rect == winapi.Rect(100 + round(10 * dpr), 200 + round(20 * dpr), round(100 * dpr), round(50 * dpr))
    assert not visible_at_callback and not manager.pickers      # closed before the screenshot is taken
    assert not cancelled

    manager.pick(on_done=done.append, on_cancel=lambda: cancelled.append(1))
    QTest.keyClick(manager.pickers[0], Qt.Key.Key_Escape)
    assert cancelled == [1] and len(done) == 1 and not manager.pickers

    manager.pick(on_done=done.append, on_cancel=lambda: cancelled.append(1))
    QTest.mouseClick(manager.pickers[0], Qt.MouseButton.LeftButton, pos=QPoint(30, 30))   # a click cancels
    assert cancelled == [1, 1] and len(done) == 1


def test_hotkey_filter_dispatches_each_binding(qt, monkeypatch):
    """Two global hotkeys (Ctrl+Alt+F whole screen, Ctrl+Alt+R region) share one native event filter."""
    import ctypes
    from ctypes import wintypes

    import factcheck.ui.hotkey as hk

    monkeypatch.setattr(hk.winapi, "register_hotkey", lambda _id, _spec: True)   # the running app holds the real keys
    monkeypatch.setattr(hk.winapi, "unregister_hotkey", lambda _id: None)
    hits = []
    hotkeys = hk.HotkeyFilter([("ctrl+alt+f", lambda: hits.append("screen")), ("ctrl+alt+r", lambda: hits.append("region"))])
    msg = wintypes.MSG()
    msg.message, msg.wParam = hk.winapi.WM_HOTKEY, hk.HOTKEY_ID + 1
    assert hotkeys.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg)) == (True, 0)
    msg.wParam = hk.HOTKEY_ID
    hotkeys.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
    msg.wParam = 0x1234                                        # someone else's hotkey: not ours
    assert hotkeys.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg)) == (False, 0)
    assert hits == ["region", "screen"]
    hotkeys.close()


def test_panel_check_all_presses_every_check_button(qt):
    """User (2026-11-13): a "Check all claims" button that presses every "Check this claim" (and "Try again") button."""
    from factcheck.ui.panel import Panel

    state = UiState()
    src = SourceInfo(kind="desktop", source_id="screen:x", app="screen", title="DISPLAY1")
    claims = [Claim(id=f"c{i}", text=f"Claim {i}.", span="s", block_id="b", source=src, status=st)
              for i, st in enumerate(["pending", "pending", "error", "checking", "done"])]
    state.on_event({"type": "claims", "source_id": "screen:x", "claims": claims})
    state.on_event({"type": "verdict", "source_id": "screen:x", "claim_id": "c4", "verdict": verdict()})
    checked = []
    panel = Panel(state, on_check=checked.append, status_text=lambda: "")
    assert panel.check_all.text() == "Check all claims (3)" and not panel.check_all.isHidden()
    panel.check_all.click()
    assert checked == ["c0", "c1", "c2"]                        # pending and failed ones, not checking/done
    for cid in checked:
        state.on_event({"type": "status", "source_id": "screen:x", "claim_id": cid, "status": "checking"})
    assert panel.check_all.isHidden()                           # nothing left to check
    panel.close()


def test_card_puts_the_label_pill_on_its_own_line():
    src = SourceInfo(kind="chrome", source_id="t", app="chrome", title="Joe Cahill - Wikipedia")
    claim = Claim(id="c", text="x", span="s", block_id="b", source=src)
    out = card_html(claim, verdict(label="Supported", corrected_claim=None))
    assert "</div><div><span" in out                     # the source line and the pill are separate blocks
