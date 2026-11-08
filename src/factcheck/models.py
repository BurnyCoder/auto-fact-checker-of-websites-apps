"""Data models shared by capture, pipeline, server, UI and eval (pydantic v2).

Local context: plain typed records plus the verdict rule checker.
Global context: a TextBlock (captured text) becomes Claims (extract.py), each Claim gets a Verdict
(verify.py); the server sends these as JSON to the Chrome extension and the Qt UI draws them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# The four AVeriTeC labels (https://arxiv.org/abs/2305.13117) — product and benchmark use the same set.
Label = Literal["Supported", "Refuted", "Not Enough Evidence", "Conflicting Evidence/Cherrypicking"]
LABELS: tuple[str, ...] = ("Supported", "Refuted", "Not Enough Evidence", "Conflicting Evidence/Cherrypicking")
NEEDS_CORRECTION = {"Refuted", "Conflicting Evidence/Cherrypicking"}
LABEL_COLOURS = {"Supported": "#2e9d4f", "Refuted": "#d93025", "Not Enough Evidence": "#80868b",
                 "Conflicting Evidence/Cherrypicking": "#f0a400"}


class Box(BaseModel):
    """A rectangle on screen in physical pixels (virtual-desktop coordinates), with the OCR text in it."""

    x: float
    y: float
    w: float
    h: float
    text: str = ""


class SourceInfo(BaseModel):
    """Where captured text came from."""

    kind: Literal["chrome", "desktop", "eval", "manual"]
    source_id: str                       # page/tab id or window handle: used to cancel stale work
    app: str = ""                        # e.g. "Discord.exe" or "chrome"
    title: str = ""                      # window title or page title
    url: str = ""
    captured_at: datetime = Field(default_factory=datetime.now)


class TextBlock(BaseModel):
    """One paragraph / message of captured text."""

    id: str
    text: str
    boxes: list[Box] = []                # OCR line boxes (desktop only), used to place overlay rectangles


class Claim(BaseModel):
    """An atomic, self-contained factual claim found in a block."""

    id: str
    text: str                            # decontextualised claim that is verified
    span: str                            # exact substring of the block, used for highlighting
    block_id: str
    source: SourceInfo
    check_worthy_reason: str = ""
    status: Literal["pending", "checking", "done", "skipped", "error"] = "pending"


class VerdictSource(BaseModel):
    url: str
    title: str = ""
    quote: str = ""
    supports: Literal["verdict", "correction"] = "verdict"
    in_trace: bool | None = None         # URL appeared in the agent's own search/fetch results
    quote_verified: bool | None = None   # we re-fetched the page and found the quote (None = not checked)


class Computation(BaseModel):
    command: str
    output: str = ""


class Verdict(BaseModel):
    """Model output fields + metadata we add."""

    label: Label
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_reasoning: str
    explanation: str
    corrected_claim: str | None = None
    correction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    correction_reasoning: str | None = None
    sources: list[VerdictSource] = []
    computations: list[Computation] = []
    # --- metadata (filled by verify.py) ---
    claim_id: str = ""
    claim: str = ""
    incomplete: bool = False
    flags: list[str] = []
    error: str | None = None
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    web_searches: int = 0
    tool_counts: dict[str, int] = {}
    model: str = ""
    auth: str = ""
    evidence: list[dict] = []            # compact tool trace (tool, input, urls, titles, output head) for audits


def rule_violations(v: Verdict, require_sources: bool = True) -> list[str]:
    """Rules the JSON schema cannot express; a non-empty list triggers one repair pass, then an 'incomplete' flag.

    `require_sources=False` is used only for the closed-book benchmark condition (no web access, so
    sources cannot be required).
    """
    problems = []
    if not v.confidence_reasoning.strip():
        problems.append("confidence_reasoning must explain the confidence")
    verdict_sources = [s for s in v.sources if s.supports == "verdict"]
    # A computation the agent actually ran in the sandbox is valid evidence for claims settled by arithmetic
    # (e.g. compound interest); found live on 2026-11-08 when a correct "Supported" was otherwise forced to NEE.
    if require_sources and v.label != "Not Enough Evidence" and not verdict_sources and not v.computations:
        problems.append("give at least one source with supports='verdict' or a computation you ran "
                        "(or use 'Not Enough Evidence')")
    if v.label in NEEDS_CORRECTION:
        if not (v.corrected_claim or "").strip():
            problems.append(f"label '{v.label}' requires corrected_claim (the accurate version of the statement)")
        if v.correction_confidence is None or not (v.correction_reasoning or "").strip():
            problems.append("a correction needs correction_confidence and correction_reasoning")
        if require_sources and not [s for s in v.sources if s.supports == "correction"]:
            problems.append("a correction needs at least one source with supports='correction'")
    return problems
