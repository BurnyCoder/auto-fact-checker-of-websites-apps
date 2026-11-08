"""H3 stimuli: render a claim inside a generic chat or article layout and screenshot it (headless Chromium).

Local context: deterministic HTML per item (layout x font size from a balanced design), a Playwright
screenshot as BGRA pixels, and the "ideal" text blocks (one per DOM element) for the clean arm.
Global context: the OCR arm feeds the pixels through the production capture code (capture/ocr.py +
capture/layout.py); the clean arm feeds the ideal blocks. Both then use the production extractor and the
closed-book verifier, so the only difference between arms is the capture channel (OCR + line grouping).
Deviation from the original plan (recorded in the preregistration): pages are rendered headless instead of
in a real Chrome window on the user's monitors, because screen access was declined.
"""

from __future__ import annotations

import html
import io
import random

import numpy as np
from PIL import Image

from factcheck.models import TextBlock

LAYOUTS = ("chat", "article")
FONT_PX = (13, 15, 18)
NAMES = ["jordan_k", "maria.v", "tomasz", "aisha_r", "lee88", "nora", "devin", "kofi_a", "sam_p", "ines"]


def design(item_ids: list[str], seed: int = 2026) -> dict[str, tuple[str, int]]:
    """Balanced assignment: shuffle items, then cycle through the 6 (layout, font size) cells."""
    cells = [(lay, px) for lay in LAYOUTS for px in FONT_PX]
    order = list(item_ids)
    random.Random(seed).shuffle(order)
    return {iid: cells[i % len(cells)] for i, iid in enumerate(order)}


def page_parts(item_index: int, claim: str, layout: str) -> list[tuple[str, str]]:
    """(element role, text) in reading order — the ideal blocks of the clean arm."""
    if layout == "chat":
        name = NAMES[item_index % len(NAMES)]
        when = f"Today at {9 + item_index % 12:02d}:{(item_index * 7) % 60:02d}"
        return [("header", f"{name}  {when}"), ("message", claim)]
    return [("title", "Morning briefing"), ("byline", "By Staff Writer · 4 min read"), ("paragraph", claim)]


def page_html(item_index: int, claim: str, layout: str, font_px: int) -> str:
    parts = page_parts(item_index, claim, layout)
    esc = html.escape
    if layout == "chat":
        (_, header), (_, message) = parts
        name, when = header.split("  ", 1)
        body = (f"<div style='display:flex;gap:12px;padding:16px'><div style='width:40px;height:40px;border-radius:50%;"
                f"background:#9aa5b1;flex:none'></div><div><div><b>{esc(name)}</b>"
                f"<span style='color:#888;font-size:{max(11, font_px - 3)}px;margin-left:8px'>{esc(when)}</span></div>"
                f"<div style='margin-top:3px'>{esc(message)}</div></div></div>")
        style = f"font-family:'Segoe UI',sans-serif;font-size:{font_px}px;background:#f7f7f8;color:#111"
    else:
        (_, title), (_, byline), (_, para) = parts
        body = (f"<div style='padding:24px 32px'><h1 style='font-size:{font_px + 10}px;margin:0 0 6px'>{esc(title)}</h1>"
                f"<div style='color:#666;font-size:{max(11, font_px - 2)}px;margin-bottom:14px'>{esc(byline)}</div>"
                f"<p style='line-height:1.5;margin:0'>{esc(para)}</p></div>")
        style = f"font-family:Georgia,serif;font-size:{font_px}px;background:#fff;color:#222"
    return f"<!doctype html><html><body style=\"margin:0;width:900px;{style}\">{body}</body></html>"


def ideal_blocks(item_index: int, claim: str, layout: str) -> list[TextBlock]:
    return [TextBlock(id=f"e{i}", text=text) for i, (_role, text) in enumerate(page_parts(item_index, claim, layout))]


async def screenshot_bgra(page, item_index: int, claim: str, layout: str, font_px: int) -> np.ndarray:
    """Render with a Playwright page (viewport 900 wide, device scale 1) and return BGRA pixels."""
    await page.set_content(page_html(item_index, claim, layout, font_px))
    png = await page.screenshot(full_page=True)
    rgba = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
    return rgba[:, :, [2, 1, 0, 3]].copy()
