"""SQLite verdict cache keyed by the normalised claim text, with a fuzzy fallback.

Local context: get/put verdict JSON by the normalised claim text (case, whitespace and punctuation ignored).
Exact keys only: a fuzzy fallback (token_sort_ratio >= 95) was removed after the code review showed it matches
"rate was 3.5%" with "3.9%" and "is safe" with "is not safe".
Global context: service.py checks the cache before spending money on a verification. The eval does
NOT use the cache (every benchmark claim must be verified independently).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from factcheck.models import Verdict
from factcheck.pipeline.segment import normalize_claim


class VerdictCache:
    def __init__(self, path: Path, ttl_days: float = 7.0):
        self.path = path
        self.ttl_s = ttl_days * 86400
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
        return Verdict.model_validate_json(row[0]) if row else None

    def put(self, claim: str, verdict: Verdict) -> None:
        if verdict.incomplete or verdict.error:         # never cache failures
            return
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?)",
                       (normalize_claim(claim), claim, verdict.model_dump_json(), time.time()))
