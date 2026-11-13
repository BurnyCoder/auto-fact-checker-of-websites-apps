"""Desktop capture tests: real Windows OCR on a rendered image, grouping, span location, policies."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont
from rapidfuzz import fuzz

from factcheck.capture import winapi
from factcheck.capture.layout import group_lines, locate_span
from factcheck.capture.ocr import OcrLine, ocr_bgra
from factcheck.capture.watcher import DesktopCapture, frames_equal
from factcheck.models import Box


def render(lines: list[tuple[int, int, str]], size=(900, 220), font_size=22) -> np.ndarray:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", font_size)
    for x, y, text in lines:
        draw.text((x, y), text, fill="black", font=font)
    return np.array(img.convert("RGBA"))[:, :, [2, 1, 0, 3]].copy()      # RGBA -> BGRA


async def test_windows_ocr_reads_rendered_text_with_offsets():
    pixels = render([(20, 20, "The Eiffel Tower is 330 metres tall and opened in 1889.")])
    lines = await ocr_bgra(pixels, origin_x=100, origin_y=50)
    assert lines and fuzz.ratio(lines[0].text, "The Eiffel Tower is 330 metres tall and opened in 1889.") > 95
    assert 110 <= lines[0].box.x <= 130 and 60 <= lines[0].box.y <= 85      # origin offset applied


def line(x, y, w, text, h=20):
    return OcrLine(text=text, box=Box(x=x, y=y, w=w, h=h, text=text))


def test_group_lines_splits_messages_and_joins_wraps():
    lines = [line(20, 20, 200, "mike_92 Today at 14:02"),
             line(20, 46, 600, "did you know the Great Wall of China is visible"),
             line(20, 70, 300, "from the Moon with the naked eye"),
             line(20, 160, 200, "sarah Today at 14:03"),            # big gap -> new block
             line(20, 186, 400, "nah thats a myth")]
    blocks = group_lines(lines)
    assert len(blocks) == 2
    assert blocks[0].text.endswith("visible from the Moon with the naked eye")
    assert len(blocks[0].boxes) == 3


def test_locate_span_across_wrapped_lines():
    blocks = group_lines([line(0, 0, 400, "aaaa the wall is visible"), line(0, 24, 400, "from the moon bbbb")])
    boxes = locate_span(blocks[0], "the wall is visible from the moon")
    assert len(boxes) == 2
    assert boxes[0].x > 0 and boxes[0].x + boxes[0].w == pytest.approx(400)   # trimmed start, full end
    assert boxes[1].x == 0 and boxes[1].w < 400


def test_frames_equal_tolerates_noise_not_changes():
    a = np.full((50, 50), 100, np.uint8)
    assert frames_equal(a, a + 1) and not frames_equal(a, a + 40) and not frames_equal(a, None)


def test_parse_hotkey():
    mods, vk = winapi.parse_hotkey("ctrl+alt+f")
    assert vk == ord("F") and mods & winapi.MOD_CONTROL and mods & winapi.MOD_ALT
    assert winapi.parse_hotkey("shift+F9")[1] == 0x78
    with pytest.raises(ValueError):
        winapi.parse_hotkey("ctrl+alt")


def test_monitors_have_device_names():
    mons = winapi.monitors()
    assert mons and all(m.device.startswith("\\\\.\\DISPLAY") and m.rect.width > 0 for m in mons)


def test_skip_rules(settings):
    cap = DesktopCapture(settings, service=None, extension_connected=lambda: True)
    win = lambda **kw: winapi.WindowInfo(**{"hwnd": 1, "title": "x", "pid": 1, "process": "discord.exe",
                                            "rect": winapi.Rect(0, 0, 800, 600), "minimized": False, **kw})
    assert cap.skip_reason(win()) is None
    assert "blocked" in cap.skip_reason(win(process="keepass.exe"))
    assert "private" in cap.skip_reason(win(title="New Tab - Google Chrome (Incognito)"))
    assert "extension" in cap.skip_reason(win(process="chrome.exe"))
    assert "minimized" in cap.skip_reason(win(minimized=True))


def test_skip_mask_follows_z_order():
    """A pixel is left out only if the topmost window covering it is a skipped one (windows listed topmost first)."""
    from factcheck.capture.watcher import skip_mask

    win = lambda hwnd, x, y, w, h: winapi.WindowInfo(hwnd=hwnd, title="", pid=1, process="a.exe",
                                                     rect=winapi.Rect(x, y, w, h), minimized=False)
    top = win(1, 100, 100, 50, 50)           # normal window covering part of the skipped one
    skipped = win(2, 80, 80, 100, 100)       # e.g. our own panel
    bottom = win(3, 0, 0, 1000, 1000)        # e.g. the desktop
    mask = skip_mask([top, skipped, bottom], winapi.Rect(0, 0, 200, 200), lambda w: w.hwnd == 2)
    assert mask[90, 90] and mask[170, 170]   # visible part of the skipped window
    assert not mask[120, 120]                # covered by the window above it
    assert not mask[10, 10]                  # the desktop
    offset = skip_mask([skipped], winapi.Rect(100, 100, 200, 200), lambda w: True)   # monitor not at the origin
    assert offset[0, 0] and offset[79, 79] and not offset[80, 80]
    overlay = winapi.WindowInfo(hwnd=4, title="", pid=1, process="nvidia overlay.exe", rect=winapi.Rect(0, 0, 200, 200),
                                minimized=False, see_through=True)       # layered + never activated, e.g. GeForce
    assert skip_mask([overlay, skipped], winapi.Rect(0, 0, 200, 200), lambda w: w.hwnd == 2)[90, 90]   # hides nothing
    assert skip_mask([overlay, bottom], winapi.Rect(0, 0, 200, 200), lambda w: w.hwnd == 4)[10, 10]   # blocked: out


def test_visible_windows_lists_real_windows():
    wins = winapi.visible_windows()
    assert wins and all(w.rect.width > 0 and w.rect.height > 0 and not w.minimized for w in wins)
    assert len({w.hwnd for w in wins}) == len(wins)


async def test_hotkey_capture_leaves_out_own_and_blocked_windows(settings, monkeypatch):
    """Regression (2026-11-13 live check): the hotkey path OCR'd whole monitors, so it fact-checked our own panel; it
    must black out the same windows continuous mode skips (own, blocked apps, private, Chrome with the extension)."""
    import os

    import factcheck.capture.watcher as watcher_mod

    pixels = render([(20, 20, "Coffee is the second most traded commodity after oil."),
                     (20, 120, "Panel says the Jacobian conjecture is false."),
                     (20, 170, "Vault password hunter2 for the bank.")])
    win = lambda pid, process, top: winapi.WindowInfo(hwnd=top, title="w", pid=pid, process=process,
                                                      rect=winapi.Rect(0, top, 900, 45), minimized=False)
    monkeypatch.setattr(watcher_mod.winapi, "monitors",
                        lambda: [winapi.Monitor(device="\\.\\DISPLAY9", rect=winapi.Rect(0, 0, 900, 220), primary=True)])
    monkeypatch.setattr(watcher_mod.winapi, "visible_windows",
                        lambda: [win(os.getpid(), "python.exe", 110), win(4242, "keepass.exe", 160)])
    monkeypatch.setattr(watcher_mod, "grab", lambda left, top, w, h: pixels)

    class Service:                                   # only the layout matters here: no extraction
        def emit(self, _e): pass
        async def submit(self, *_a, **_kw): pass

    cap = DesktopCapture(settings, Service(), extension_connected=lambda: False)
    await cap.capture_now()
    text = " ".join(b.text for b in cap.layouts["screen:\\.\\DISPLAY9"])
    assert "Coffee" in text and "Jacobian" not in text and "hunter2" not in text


async def test_hotkey_capture_path_with_rendered_screen(settings, monkeypatch):
    """capture_now on a fake 'monitor' (rendered image instead of a screenshot): OCR -> blocks -> service -> events."""
    import asyncio

    import factcheck.capture.watcher as watcher_mod
    from factcheck.pipeline.fake import FakeRunner
    from factcheck.pipeline.service import FactCheckService

    pixels = render([(20, 20, "sarah Today at 14:03"),
                     (20, 50, "Coffee is the second most traded commodity after oil since 2019.")])
    monkeypatch.setattr(watcher_mod.winapi, "monitors",
                        lambda: [winapi.Monitor(device="\\.\\DISPLAY9", rect=winapi.Rect(0, 0, 900, 220), primary=True)])
    monkeypatch.setattr(watcher_mod, "grab", lambda left, top, w, h: pixels)
    monkeypatch.setattr(watcher_mod.winapi, "visible_windows", list)     # independent of the real desktop
    settings.factcheck_fake_llm = True
    service = FactCheckService(settings, FakeRunner(settings))
    events = []
    service.listeners.append(events.append)
    service.start()
    cap = DesktopCapture(settings, service, extension_connected=lambda: False)
    assert await cap.capture_now() >= 1
    async with asyncio.timeout(10):
        while not any(e["type"] == "verdict" for e in events):
            await asyncio.sleep(0.05)
    layout = next(e for e in events if e["type"] == "layout")
    claims = next(e for e in events if e["type"] == "claims")["claims"]
    assert layout["source_id"] == "screen:\\.\\DISPLAY9"
    assert any("Coffee" in c.span for c in claims)
    await service.stop()


def test_merge_rows_joins_username_and_timestamp():
    from factcheck.capture.layout import merge_rows
    rows = merge_rows([line(72, 70, 70, "mike_92", h=17), line(150, 72, 100, "Today at 14:02", h=13),
                       line(72, 96, 600, "did you know the wall is visible")])
    assert [r.text for r in rows] == ["mike_92 Today at 14:02", "did you know the wall is visible"]
    blocks = group_lines([line(72, 70, 70, "mike_92", h=17), line(150, 72, 100, "Today at 14:02", h=13),
                          line(72, 96, 600, "did you know the wall is visible")])
    assert len(blocks) == 1
