"""Claim extraction: captured text blocks -> atomic, self-contained, check-worthy claims with exact spans.

Local context: one tool-less Haiku call per batch of blocks, then span alignment so every claim can be
highlighted at the right place on screen.
Global context: service.py calls `extract_claims` for each capture; the CheckThat! benchmark (H4)
scores this exact function.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date

from rapidfuzz import fuzz

from factcheck.models import Claim, SourceInfo, TextBlock
from factcheck.pipeline.agent import AgentRunner
from factcheck.pipeline.prompts import (
    EXTRACT_SCHEMA,
    EXTRACT_SYSTEM,
    build_extract_prompt,
    context_lines,
    per_sentence_schema,
    sentence_units,
)
from factcheck.pipeline.segment import normalize_claim

log = logging.getLogger(__name__)


def align_span(span: str, text: str, threshold: float = 90.0) -> str | None:
    """Return the exact substring of `text` that the model meant by `span`, or None if it cannot be found.

    Exact match first; otherwise rapidfuzz's partial_ratio_alignment gives the best-matching window
    (https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html#partial-ratio-alignment).
    """
    span = span.strip()
    if not span:
        return None
    if span in text:
        return span
    hit = fuzz.partial_ratio_alignment(span, text)
    if hit is None or hit.score < threshold:
        return None
    return text[hit.dest_start:hit.dest_end].strip() or None


def claim_id(source: SourceInfo, claim_text: str) -> str:
    return hashlib.sha1(f"{source.source_id}|{normalize_claim(claim_text)}".encode()).hexdigest()[:12]


async def extract_claims(blocks: list[TextBlock], source: SourceInfo, runner: AgentRunner, *, purpose: str,
                         max_claims: int | None = None) -> list[Claim]:
    """Extract claims from `blocks`; drops claims whose span cannot be located in their block.
    `max_claims=None` (the app): every claim, most important first; the service verifies the first
    MAX_AUTO_CHECKS_PER_BATCH and the rest wait for the user's Check button (user decision, 2026-11-13).
    The eval passes its preregistered cap explicitly, so its prompts are unchanged."""
    blocks = [b for b in blocks if b.text.strip()]
    if not blocks:
        return []
    context = context_lines(app=source.app, title=source.title, url=source.url, when=date.today().isoformat())
    units = sentence_units(blocks) if max_claims is None else []
    if max_claims is None and not units:
        return []
    schema = per_sentence_schema([sid for sid, _, _ in units]) if max_claims is None else EXTRACT_SCHEMA
    run = await runner.run(build_extract_prompt(blocks, context, max_claims), kind="extract", purpose=purpose,
                           system_prompt=EXTRACT_SYSTEM, schema=schema, tools=[], max_turns=3,
                           worst_case_usd=0.03, timeout_s=runner.settings.extract_timeout_s,
                           meta={"source_id": source.source_id, "n_blocks": len(blocks), "n_sentences": len(units)})
    if not run.ok:
        log.warning("extraction failed: %s", run.error)
        return []
    if max_claims is None:                              # per-sentence answer -> one list, most important first
        answer = run.structured or {}
        items = [{**item, "block_id": block_id} for sid, block_id, _ in units for item in answer.get(sid) or []]
        items.sort(key=lambda item: item.get("importance", 2))   # stable: reading order within each importance
        log.info("extracted %d claims from %d sentences in %d blocks", len(items), len(units), len(blocks))
    else:
        items = (run.structured or {}).get("claims", [])[:max_claims]
    by_id = {b.id: b for b in blocks}
    claims: list[Claim] = []
    seen: set[str] = set()
    for item in items:
        block = by_id.get(str(item.get("block_id", "")))
        if block is None and len(blocks) == 1:          # tolerate a wrong id when there is only one block
            block = blocks[0]
        if block is None:
            log.info("dropping claim with unknown block id: %s", item)
            continue
        span = align_span(str(item.get("span", "")), block.text)
        if span is None:
            log.info("dropping claim whose span is not in its block: %r", item.get("span"))
            continue
        text = str(item.get("claim", "")).strip() or span
        cid = claim_id(source, text)
        if cid in seen:
            continue
        seen.add(cid)
        claims.append(Claim(id=cid, text=text, span=span, block_id=block.id, source=source,
                            check_worthy_reason=str(item.get("reason", ""))))
    return claims
