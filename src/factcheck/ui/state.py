"""UI-side state on the Qt main thread: claims, verdicts and screen layouts, fed by backend events.

Local context: `Bridge` turns backend-thread listener calls into a Qt signal (queued across threads);
`UiState` keeps the latest claims/verdicts/layouts and computes what the overlay should draw.
Global context: the panel and the overlays only read from UiState and redraw on its `changed` signal,
so they never touch backend objects directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from factcheck.capture.layout import locate_in_blocks
from factcheck.models import Box, Claim, TextBlock, Verdict

SCREEN_LAYOUT_TTL_S = 60.0          # hotkey (whole-screen) boxes disappear this long after the screen's last result
                                    # (not after the capture: verdicts take 20-60 s) or on any frame change


class Bridge(QObject):
    """Backend listener -> Qt signal. Emitting from the backend thread is safe; delivery is queued."""

    event = Signal(object)

    def listener(self, event: dict) -> None:
        self.event.emit(event)


@dataclass
class OverlayItem:
    claim_id: str
    boxes: list[Box]
    label: str | None             # None while not yet verified
    status: str
    confidence: float | None


class UiState(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.claims: dict[str, Claim] = {}
        self.verdicts: dict[str, Verdict] = {}
        self.order: list[str] = []                       # claim ids, oldest first
        self.layouts: dict[str, list[TextBlock]] = {}
        self.layout_time: dict[str, float] = {}
        self.stale: set[str] = set()
        self.messages: list[str] = []
        self.expiry = QTimer(self)                       # drop old hotkey boxes on time, even when no event arrives
        self.expiry.timeout.connect(self.expire)
        self.expiry.start(1000)

    def on_event(self, e: dict) -> None:
        kind, sid = e.get("type"), e.get("source_id")
        if sid in self.layout_time and kind in ("claims", "verdict", "status"):
            self.layout_time[sid] = time.monotonic()     # restart the clock: boxes stay readable after each result
        if kind == "claims":
            for c in e["claims"]:
                if c.id not in self.claims:
                    self.order.append(c.id)
                self.claims[c.id] = c
        elif kind == "verdict":
            self.verdicts[e["claim_id"]] = e["verdict"]
            if e["claim_id"] in self.claims:
                self.claims[e["claim_id"]].status = "done"
        elif kind == "status" and e.get("claim_id") in self.claims:
            self.claims[e["claim_id"]].status = e["status"]
        elif kind == "status" and e.get("status") == "error":
            self.messages.append(e.get("message", "error"))
        elif kind == "layout":
            self.layouts[sid] = e["blocks"]
            self.layout_time[sid] = time.monotonic()
            self.stale.discard(sid)
        elif kind == "frame_changed":
            self.stale.add(sid)
            self.stale.update(s for s in self.layouts if s.startswith("screen:"))   # whole-screen boxes go stale too
        else:
            return
        if len(self.order) > 1000:                       # bound memory and repaint cost in long sessions
            for cid in self.order[:-800]:
                self.claims.pop(cid, None)
                self.verdicts.pop(cid, None)
            self.order = self.order[-800:]
        self.changed.emit()

    def expire(self) -> None:
        """Forget whole-screen layouts with no result for SCREEN_LAYOUT_TTL_S (the screen has likely changed since)."""
        now = time.monotonic()
        old = [s for s in self.layouts if s.startswith("screen:") and now - self.layout_time.get(s, now) > SCREEN_LAYOUT_TTL_S]
        for sid in old:
            self.layouts.pop(sid)
            self.layout_time.pop(sid, None)
        if old:
            self.changed.emit()

    def overlay_items(self) -> list[OverlayItem]:
        """Boxes to draw now: claims whose source layout is current and where the span can be located."""
        items = []
        for cid in self.order:
            claim = self.claims[cid]
            sid = claim.source.source_id
            if claim.source.kind != "desktop" or sid in self.stale or sid not in self.layouts:
                continue
            boxes = locate_in_blocks(self.layouts[sid], claim.span, claim.block_id)
            if not boxes:
                continue
            v = self.verdicts.get(cid)
            items.append(OverlayItem(cid, boxes, v.label if v else None, claim.status, v.confidence if v else None))
        return items
