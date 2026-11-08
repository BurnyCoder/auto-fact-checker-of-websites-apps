"""Desktop-path demo with real Haiku on a RENDERED chat screenshot (no access to the real screen needed).

Usage:  uv run python scripts/demo_overlay.py
Flow: render a generic chat window image -> DesktopCapture.capture_now (real Windows OCR, paragraph grouping)
-> real extraction + verification (Haiku, web, sandbox) -> draw the real overlay and side panel offscreen
-> docs/images/overlay-demo.png and docs/images/panel-demo.png.
Global context: the screen capture call is replaced by the rendered image; everything after it is the
production code path. Spends API-equivalent budget under the app's ledger purposes.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")   # the offscreen platform has no font database otherwise

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

import factcheck.capture.watcher as watcher_mod
from factcheck.capture import winapi
from factcheck.capture.watcher import DesktopCapture
from factcheck.config import load_settings
from factcheck.log import setup_logging
from factcheck.pipeline.agent import AgentRunner
from factcheck.pipeline.budget import BudgetLedger
from factcheck.pipeline.cache import VerdictCache
from factcheck.pipeline.service import FactCheckService
from factcheck.ui.overlay import ScreenOverlay
from factcheck.ui.panel import Panel
from factcheck.ui.state import UiState

ROOT = Path(__file__).resolve().parents[1]
W, H = 1000, 520
MESSAGES = [
    ("mike_92", "Today at 14:02", "lol did you know the Great Wall of China is visible from the Moon with the naked eye"),
    ("sarah", "Today at 14:03", "nah that's a myth. also Mount Everest is 8,849 metres tall, they re-measured it in 2020"),
    ("mike_92", "Today at 14:05", "ok but a 7% yearly return doubles your money in about 10 years"),
    ("sarah", "Today at 14:06", "whatever, pizza is better than burgers anyway. game tonight?"),
]


def render_chat() -> Image.Image:
    """A plain, generic chat layout (not a copy of any real app's design)."""
    img = Image.new("RGB", (W, H), "#f7f7f8")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 44], fill="#e9e9ee")
    d.text((20, 12), "# general", fill="#333", font=ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 18))
    name_f = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 17)
    meta_f = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13)
    text_f = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 19)
    y = 70
    for name, when, text in MESSAGES:
        d.ellipse([20, y, 56, y + 36], fill="#9aa5b1")
        d.text((72, y - 2), name, fill="#222", font=name_f)
        d.text((72 + d.textlength(name, font=name_f) + 10, y + 2), when, fill="#888", font=meta_f)
        d.text((72, y + 24), text, fill="#111", font=text_f)
        y += 105
    return img


async def run_pipeline(settings, image: Image.Image) -> list[dict]:
    pixels = np.array(image.convert("RGBA"))[:, :, [2, 1, 0, 3]].copy()
    watcher_mod.winapi.monitors = lambda: [winapi.Monitor(device=r"\\.\DEMO", rect=winapi.Rect(0, 0, W, H), primary=True)]
    watcher_mod.grab = lambda left, top, w, h: pixels
    ledger = BudgetLedger(settings.ledger_path, settings.total_budget_usd, settings.daily_app_budget_usd)
    service = FactCheckService(settings, AgentRunner(settings, ledger), VerdictCache(settings.data_dir / "verdicts.sqlite"))
    events: list[dict] = []
    service.listeners.append(events.append)
    service.start()
    await DesktopCapture(settings, service, lambda: False).capture_now()
    async with asyncio.timeout(400):
        while True:
            await asyncio.sleep(1)
            claims = [c for e in events if e["type"] == "claims" for c in e["claims"]]
            done = {e["claim_id"] for e in events if e["type"] == "verdict"}
            if claims and all(c.id in done or c.status == "pending" for c in claims):
                break
    await service.stop()
    print(f"ledger total: ${ledger.spent_total():.4f}")
    return events


def main() -> None:
    settings = load_settings()
    setup_logging(settings.logs_dir, name="demo-overlay")
    image = render_chat()
    events = asyncio.run(run_pipeline(settings, image))
    app = QApplication.instance() or QApplication([])
    state = UiState()
    for e in events:
        state.on_event(e)
    for e in events:
        if e["type"] == "verdict":
            v = e["verdict"]
            print(f"- {v.label} {v.confidence:.2f} | {v.claim} | correction={v.corrected_claim!r} | ${v.cost_usd:.4f}")
    screen = app.primaryScreen()
    overlay = ScreenOverlay(screen, winapi.Monitor(device="demo", rect=winapi.Rect(0, 0, W, H), primary=True), state, True)
    overlay.resize(W, H)
    base = QImage(image.tobytes("raw", "RGB"), W, H, 3 * W, QImage.Format.Format_RGB888).copy()
    painter = QPainter(base)
    painter.drawPixmap(0, 0, overlay.grab())
    painter.end()
    out = ROOT / "docs" / "images"
    base.save(str(out / "overlay-demo.png"))
    panel = Panel(state, on_check=lambda _cid: None, status_text=lambda: "Demo run · real Haiku verdicts on a rendered chat")
    panel.resize(480, 900)
    panel.grab().save(str(out / "panel-demo.png"))
    print("saved", out / "overlay-demo.png", out / "panel-demo.png")


if __name__ == "__main__":
    main()
