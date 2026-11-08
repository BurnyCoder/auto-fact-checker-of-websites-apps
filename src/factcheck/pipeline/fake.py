"""Deterministic stand-in for AgentRunner (FACTCHECK_FAKE_LLM=true): zero cost, no network.

Local context: produces schema-valid extraction and verification outputs from simple rules.
Global context: lets the UI, the Chrome extension and end-to-end tests run without spending budget.
Extraction: every sentence containing a digit becomes a claim. Verification: the label is chosen from
a hash of the claim so the same claim always gets the same verdict.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from factcheck.config import Settings
from factcheck.models import LABELS
from factcheck.pipeline.agent import AgentRun
from factcheck.pipeline.segment import split_sentences

_BLOCK_RE = re.compile(r"^\[(?P<id>[^\]]+)\] (?P<text>.*)$", re.MULTILINE)


class FakeRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.auth = "fake"

    async def run(self, prompt: str, *, kind: str, **_: object) -> AgentRun:
        run = AgentRun(run_id=uuid.uuid4().hex[:10], ok=True, subtype="success", auth="fake", num_turns=1)
        if kind == "extract":
            claims = []
            for m in _BLOCK_RE.finditer(prompt):
                for sentence in split_sentences(m.group("text")):
                    if any(ch.isdigit() for ch in sentence):
                        claims.append({"block_id": m.group("id"), "span": sentence, "claim": sentence,
                                       "reason": "contains a number"})
            run.structured = {"claims": claims}
        else:
            claim = re.search(r"<claim>(.*?)</claim>", prompt, re.DOTALL)
            text = claim.group(1) if claim else prompt
            label = LABELS[int(hashlib.sha1(text.encode()).hexdigest(), 16) % len(LABELS)]
            needs_fix = label in ("Refuted", "Conflicting Evidence/Cherrypicking")
            run.structured = {
                "label": label, "confidence": 0.62,
                "confidence_reasoning": "FAKE MODE: deterministic placeholder, not a real check.",
                "explanation": f"FAKE MODE verdict for testing the interface: {label}.",
                "corrected_claim": f"(fake correction of) {text}" if needs_fix else None,
                "correction_confidence": 0.5 if needs_fix else None,
                "correction_reasoning": "FAKE MODE placeholder." if needs_fix else None,
                "sources": [{"url": "https://example.com/", "title": "Example Domain", "quote": "Example Domain",
                             "supports": "verdict"}]
                + ([{"url": "https://example.org/", "title": "Example", "quote": "Example Domain",
                     "supports": "correction"}] if needs_fix else []),
                "computations": [],
            }
        return run
