"""DesktopCapture: hotkey (OCR every monitor now) and continuous (OCR the foreground window when it settles).

Local context: screenshot -> cheap change detection -> OCR -> paragraph blocks -> FactCheckService.submit,
plus `layout` / `frame_changed` events so the overlay can place and hide its boxes.
Global context: runs as a task on the backend event loop (backend.py). Privacy rules live here: paused
state, blocked apps (password managers, banking, ...), our own windows, incognito/private windows, and
Chrome windows while the extension is connected (the extension handles those with exact page text).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import numpy as np

from factcheck.capture import winapi
from factcheck.capture.layout import group_lines
from factcheck.capture.ocr import grab, ocr_bgra
from factcheck.config import Settings
from factcheck.models import SourceInfo
from factcheck.pipeline.service import FactCheckService

log = logging.getLogger(__name__)
PRIVATE_TITLES = ("incognito", "inprivate", "private browsing")
CHROME_PROCESSES = {"chrome.exe", "msedge.exe", "brave.exe"}


def frames_equal(a: np.ndarray | None, b: np.ndarray | None, tol: float = 1.5) -> bool:
    """Mean absolute difference of downsampled grayscale frames below `tol` (0-255 scale)."""
    if a is None or b is None or a.shape != b.shape:
        return False
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)))) < tol


def thumbnail(pixels: np.ndarray, step: int = 8) -> np.ndarray:
    return pixels[::step, ::step, 1]                       # green channel ~ luminance, every 8th pixel


class DesktopCapture:
    def __init__(self, settings: Settings, service: FactCheckService, extension_connected: Callable[[], bool]):
        self.settings = settings
        self.service = service
        self.extension_connected = extension_connected
        self.paused = False
        self.continuous_enabled = settings.factcheck_mode in ("continuous", "both")
        self.layouts: dict[str, list] = {}                 # source_id -> latest TextBlocks (with boxes)
        self.current_source: str | None = None

    # ---------------------------------------------------------------- policy
    def skip_reason(self, win: winapi.WindowInfo) -> str | None:
        if win.is_own:
            return "own window"
        if win.minimized or win.rect.width < 80 or win.rect.height < 60:
            return "minimized/tiny"
        if win.process in self.settings.blocked_app_list:
            return f"blocked app {win.process}"
        if any(p in win.title.lower() for p in PRIVATE_TITLES):
            return "private window"
        if win.process in CHROME_PROCESSES and self.extension_connected():
            return "browser handled by the extension"
        return None

    # ---------------------------------------------------------------- OCR helpers
    async def _ocr_region(self, rect: winapi.Rect, prefix: str):
        pixels = await asyncio.to_thread(grab, rect.left, rect.top, rect.width, rect.height)
        lines = await ocr_bgra(pixels, rect.left, rect.top)
        return pixels, group_lines(lines, prefix=prefix)

    def _publish_layout(self, source_id: str, blocks) -> None:
        self.layouts[source_id] = blocks
        self.service.emit({"type": "layout", "source_id": source_id, "blocks": blocks})

    # ---------------------------------------------------------------- hotkey path
    async def capture_now(self) -> int:
        """OCR every monitor and check what is visible (forced: re-extracts even if seen). Returns #blocks."""
        if self.paused:
            return 0
        total = 0
        for mon in winapi.monitors():
            source_id = f"screen:{mon.device}"
            try:
                _, blocks = await self._ocr_region(mon.rect, prefix=f"s{mon.device[-1]}-")
            except Exception:
                log.exception("OCR failed on %s", mon.device)
                continue
            self._publish_layout(source_id, blocks)
            source = SourceInfo(kind="desktop", source_id=source_id, app="screen", title=mon.device)
            asyncio.create_task(self.service.submit(source, blocks, auto=True, force=True))
            total += len(blocks)
        log.info("hotkey capture: %d text blocks on %d monitor(s)", total, len(winapi.monitors()))
        return total

    # ---------------------------------------------------------------- continuous path
    async def run_continuous(self) -> None:
        prev_thumb = last_ocr_thumb = None
        prev_source = None
        stable = 0
        while True:
            await asyncio.sleep(self.settings.capture_interval_s)
            if self.paused or not self.continuous_enabled:
                continue
            try:
                win = winapi.foreground_window()
                if win is None or self.skip_reason(win):
                    if self.current_source:
                        self.service.emit({"type": "frame_changed", "source_id": self.current_source})
                    self.current_source = None
                    continue
                source_id = f"win:{win.hwnd}"
                pixels = await asyncio.to_thread(grab, win.rect.left, win.rect.top, win.rect.width, win.rect.height)
                thumb = thumbnail(pixels)
                if source_id != prev_source or not frames_equal(thumb, prev_thumb):
                    stable = 0                                    # moving/scrolling: hide boxes, wait to settle
                    self.service.emit({"type": "frame_changed", "source_id": prev_source or source_id})
                else:
                    stable += 1
                prev_thumb, prev_source = thumb, source_id
                self.current_source = source_id
                if stable >= 1 and not frames_equal(thumb, last_ocr_thumb):   # settled and new content
                    lines = await ocr_bgra(pixels, win.rect.left, win.rect.top)
                    blocks = group_lines(lines, prefix="w")
                    last_ocr_thumb = thumb
                    self._publish_layout(source_id, blocks)
                    source = SourceInfo(kind="desktop", source_id=source_id, app=win.process, title=win.title)
                    asyncio.create_task(self.service.submit(source, blocks, auto=True))
                elif stable == 1 and source_id in self.layouts:      # just settled on known content: show boxes again
                    self.service.emit({"type": "layout", "source_id": source_id, "blocks": self.layouts[source_id]})
            except Exception:
                log.exception("continuous capture iteration failed")
