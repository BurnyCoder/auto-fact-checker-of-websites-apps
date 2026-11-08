"""SQLite verdict cache keyed by the normalised claim text, with a fuzzy fallback.

Local context: get/put verdict JSON; exact key first, then the closest recent claim above a high
similarity threshold (rapidfuzz token_sort_ratio >= 95), so trivial rewordings reuse a verdict.
Global context: service.py checks the cache before spending money on a verification. The eval does
NOT use the cache (every benchmark claim must be verified independently).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from rapidfuzz import fuzz, process

from factcheck.models import Verdict
from factcheck.pipeline.segment import normalize_claim


class VerdictCache:
    def __init__(self, path: Path, ttl_days: float = 7.0, fuzzy_cutoff: float = 95.0, fuzzy_window: int = 2000):
        self.path = path
        self.ttl_s = ttl_days * 86400
        self.fuzzy_cutoff = fuzzy_cutoff
        self.fuzzy_window = fuzzy_window
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, claim TEXT, verdict TEXT, created REAL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, claim: str) -> Verdict | None:
        key = normalize_claim(claim)
        cutoff = time.time() - self.ttl_s
        with self._lock, self._connect() as db:
            row = db.execute("SELECT verdict FROM verdicts WHERE key=? AND created>?", (key, cutoff)).fetchone()
            if row is None:                             # fuzzy fallback over the most recent entries
                recent = db.execute("SELECT key, verdict FROM verdicts WHERE created>? ORDER BY created DESC LIMIT ?",
                                    (cutoff, self.fuzzy_window)).fetchall()
                if recent:
                    match = process.extractOne(key, [r[0] for r in recent], scorer=fuzz.token_sort_ratio,
                                               score_cutoff=self.fuzzy_cutoff)
                    if match is not None:
                        row = (recent[match[2]][1],)
        return Verdict.model_validate_json(row[0]) if row else None

    def put(self, claim: str, verdict: Verdict) -> None:
        if verdict.incomplete or verdict.error:         # never cache failures
            return
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?)",
                       (normalize_claim(claim), claim, verdict.model_dump_json(), time.time()))
