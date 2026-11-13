"""DesktopCapture: hotkey (OCR every monitor now) and continuous (OCR the foreground window when it settles).

Local context: screenshot -> cheap change detection -> OCR -> paragraph blocks -> FactCheckService.submit,
plus `layout` / `frame_changed` events so the overlay can place and hide its boxes.
Global context: runs as a task on the backend event loop (backend.py). Privacy rules live here: paused
state, blocked apps (password managers, banking, ...), our own windows, incognito/private windows, and
Chrome windows while the extension is connected (the extension handles those with exact page text).
Continuous mode skips such a foreground window; the hotkey path, which reads whole monitors, blacks out the parts
of the screen where such a window is on top before OCR (`skip_mask`). A region the user drags (Ctrl+Alt+R,
ui/region.py) is an explicit request, so there only the privacy rules apply: Chrome inside it is read too.
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


def skip_mask(windows: list[winapi.WindowInfo], rect: winapi.Rect,
              skip: Callable[[winapi.WindowInfo], bool]) -> np.ndarray:
    """Boolean (height, width) mask of one monitor's pixels to leave out. `windows` is topmost first; painting them
    bottom-up means each pixel ends up owned by the topmost window covering it. A see-through window (may be
    transparent) never counts as covering, but is itself left out if `skip` says so."""
    mask = np.zeros((rect.height, rect.width), dtype=bool)
    for w in reversed(windows):
        x0, y0 = max(w.rect.left - rect.left, 0), max(w.rect.top - rect.top, 0)
        x1 = min(w.rect.left + w.rect.width - rect.left, rect.width)
        y1 = min(w.rect.top + w.rect.height - rect.top, rect.height)
        leave_out = skip(w)
        if x0 < x1 and y0 < y1 and (leave_out or not w.see_through):
            mask[y0:y1, x0:x1] = leave_out
    return mask


def _describe(windows: list[winapi.WindowInfo], reasons: dict[int, str]) -> str:
    """Log text for the windows a capture left out: process names and reasons, never window titles."""
    return ", ".join(f"{w.process} ({reasons[w.hwnd]})" for w in windows if w.hwnd in reasons) or "nothing"


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
        """Why continuous mode ignores this foreground window (None = capture it)."""
        if win.minimized or win.rect.width < 80 or win.rect.height < 60:
            return "minimized/tiny"
        return self.leave_out_reason(win)

    def privacy_reason(self, win: winapi.WindowInfo) -> str | None:
        """Why this window's text must never be read (every capture path, including a dragged region)."""
        if win.is_own:
            return "own window"
        if win.process in self.settings.blocked_app_list:
            return f"blocked app {win.process}"
        if any(p in win.title.lower() for p in PRIVATE_TITLES):
            return "private window"
        return None

    def leave_out_reason(self, win: winapi.WindowInfo) -> str | None:
        """Privacy rules plus Chrome while the extension is connected: it checks pages with exact text and
        underlines, so the automatic paths (continuous, whole-screen hotkey) leave Chrome to it."""
        if reason := self.privacy_reason(win):
            return reason
        if win.process in CHROME_PROCESSES and self.extension_connected():
            return "browser handled by the extension"
        return None

    # ---------------------------------------------------------------- OCR helpers
    async def _ocr_rect(self, rect: winapi.Rect, prefix: str, windows: list[winapi.WindowInfo], left_out: set[int]):
        """Screenshot a desktop rectangle, black out where a left-out window is on top, OCR, group into blocks."""
        pixels = await asyncio.to_thread(grab, rect.left, rect.top, rect.width, rect.height)
        mask = skip_mask(windows, rect, lambda w: w.hwnd in left_out)
        if mask.any():
            pixels = pixels.copy()                           # mss buffers are read-only
            pixels[mask] = (0, 0, 0, 255)                    # opaque black: no text for the OCR to find
        lines = await ocr_bgra(pixels, rect.left, rect.top)
        return group_lines(lines, prefix=prefix)

    def _publish_layout(self, source_id: str, blocks) -> None:
        self.layouts[source_id] = blocks
        self.service.emit({"type": "layout", "source_id": source_id, "blocks": blocks})

    # ---------------------------------------------------------------- hotkey path
    async def capture_now(self) -> int:
        """OCR every monitor and check what is visible (forced: re-extracts even if seen). Returns #blocks."""
        if self.paused:
            return 0
        total = 0
        windows = await asyncio.to_thread(winapi.visible_windows)
        reasons = {w.hwnd: r for w in windows if (r := self.leave_out_reason(w))}
        for mon in winapi.monitors():
            source_id = f"screen:{mon.device}"
            try:
                blocks = await self._ocr_rect(mon.rect, f"s{mon.device[-1]}-", windows, set(reasons))
            except Exception:
                log.exception("OCR failed on %s", mon.device)
                continue
            self._publish_layout(source_id, blocks)
            source = SourceInfo(kind="desktop", source_id=source_id, app="screen", title=mon.device)
            asyncio.create_task(self.service.submit(source, blocks, auto=True, force=True))
            total += len(blocks)
        log.info("hotkey capture: %d text blocks on %d monitor(s); left out: %s", total, len(winapi.monitors()),
                 _describe(windows, reasons))
        return total

    async def capture_region(self, rect: winapi.Rect) -> int:
        """OCR only the rectangle the user dragged (ui/region.py) and check it. Returns #blocks.
        One source, "screen:region": a new region replaces the previous one, and the `screen:` prefix gives its boxes
        the same 60 s expiry and frame-change hiding as whole-screen checks (ui/state.py)."""
        if self.paused:
            return 0
        windows = await asyncio.to_thread(winapi.visible_windows)
        reasons = {w.hwnd: r for w in windows if (r := self.privacy_reason(w))}
        try:
            blocks = await self._ocr_rect(rect, "r-", windows, set(reasons))
        except Exception:
            log.exception("OCR failed on region %s", rect)
            return 0
        source_id = "screen:region"
        self._publish_layout(source_id, blocks)
        source = SourceInfo(kind="desktop", source_id=source_id, app="screen", title="selected region")
        asyncio.create_task(self.service.submit(source, blocks, auto=True, force=True))
        log.info("region capture: %d text blocks in %s; left out: %s", len(blocks), rect, _describe(windows, reasons))
        return len(blocks)

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
