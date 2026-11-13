"""Global spend ledger with a hard cap (default $10 API-equivalent for the whole project).

Local context: price table, a cost estimator from the SDK's `usage` dict, and an append-only JSONL
ledger that refuses new calls once the cap would be exceeded.
Global context: every model call — app, tests that use the live API, pilot, eval — goes through
`BudgetLedger.reserve()` before and `BudgetLedger.record()` after (pipeline/agent.py does this), so
`budget_ledger.jsonl` (committed to git) is the single source of truth for what the project spent.
Billing is to the user's subscription when possible, so dollars here are API-equivalent estimates.
"""

from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

# USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-08).
# Our live smoke test matched the SDK's total_cost_usd exactly with these numbers (see reports/literature-review.md).
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write_5m": 0.125, "cache_write_1h": 0.20, "cache_read": 0.01},
}
WEB_SEARCH_USD = 0.01        # $10 per 1,000 searches (same pricing page)


class BudgetExceeded(RuntimeError):
    """Raised before a call that could push spending over a cap."""


class BudgetBusy(BudgetExceeded):
    """The call fits under the caps, but not while other calls hold their worst-case reservations: wait and retry.

    Found in the live Chrome test (2026-11-13): with $0.63 of a $1 daily cap spent and $0.21 reserved per check,
    the 2nd and 3rd concurrent checks were refused as "daily app budget reached" and showed as never checked."""


@contextmanager
def file_lock(path: Path):
    """Cross-process exclusive lock on `<path>.lock` (msvcrt on Windows, fcntl elsewhere).

    Needed because several processes (app, eval runs) append to the same ledger: on 2026-11-08 three concurrent
    eval processes interleaved their appends and corrupted one line (repaired from the LLM logs). msvcrt.locking
    with LK_LOCK retries for ~10 s before raising (https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)."""
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+") as fh:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def estimate_cost(model: str, usage: dict[str, Any] | None) -> float:
    """Our own cost estimate from an SDK `usage` dict (tokens by kind + server-side web searches)."""
    if not usage:
        return 0.0
    p = PRICES_PER_MTOK.get(model, PRICES_PER_MTOK["claude-haiku-5-5"])
    cache = usage.get("cache_creation") or {}
    write_1h = cache.get("ephemeral_1h_input_tokens", 0) or 0
    write_5m = cache.get("ephemeral_5m_input_tokens", 0) or 0
    if not cache:                                     # older usage shape: one number, assume 5-minute writes
        write_5m = usage.get("cache_creation_input_tokens", 0) or 0
    tokens_cost = (
        (usage.get("input_tokens", 0) or 0) * p["input"]
        + (usage.get("output_tokens", 0) or 0) * p["output"]
        + write_5m * p["cache_write_5m"]
        + write_1h * p["cache_write_1h"]
        + (usage.get("cache_read_input_tokens", 0) or 0) * p["cache_read"]
    ) / 1_000_000
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests", 0) or 0
    return round(tokens_cost + searches * WEB_SEARCH_USD, 8)


class BudgetLedger:
    """Append-only JSONL ledger with a project-wide cap and a per-day cap for app usage."""

    def __init__(self, path: Path, total_cap_usd: float, daily_app_cap_usd: float | None = None):
        self.path = path
        self.total_cap = total_cap_usd
        self.daily_app_cap = daily_app_cap_usd
        self._lock = threading.Lock()
        self._reserved = 0.0                           # worst-case cost of calls currently in flight

    def _rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with file_lock(self.path), self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]   # a corrupt line raises: fail safe, stop spending

    def spent_total(self) -> float:
        return round(sum(r["cost_usd"] for r in self._rows()), 6)

    def spent_today_app(self) -> float:
        today = date.today().isoformat()
        return round(sum(r["cost_usd"] for r in self._rows() if r["ts"].startswith(today) and r["purpose"].startswith("app")), 6)

    def remaining(self) -> float:
        return round(self.total_cap - self.spent_total() - self._reserved, 6)

    def reserve(self, worst_case_usd: float, purpose: str) -> None:
        """Claim room for one call. Raise BudgetExceeded if the spending so far leaves no room for it, or BudgetBusy
        if it only lacks room because of other calls still in flight (their reservations are released when done)."""
        with self._lock:
            caps = [("total budget", self.total_cap, self.spent_total(), "TOTAL_BUDGET_USD")]
            if purpose.startswith("app") and self.daily_app_cap is not None:
                caps.append(("daily app budget", self.daily_app_cap, self.spent_today_app(), "DAILY_APP_BUDGET_USD"))
            for name, cap, spent, env in caps:
                if spent + worst_case_usd > cap:
                    raise BudgetExceeded(f"{name} ${cap:.2f} reached (${spent:.2f} spent, a call may cost up to "
                                         f"${worst_case_usd:.2f}); raise {env} in .env to continue")
            for name, cap, spent, _ in caps:
                if spent + self._reserved + worst_case_usd > cap:
                    raise BudgetBusy(f"{name} ${cap:.2f}: waiting for ${self._reserved:.2f} reserved by calls in flight")
            self._reserved += worst_case_usd

    def release(self, worst_case_usd: float) -> None:
        with self._lock:
            self._reserved = max(0.0, self._reserved - worst_case_usd)

    def record(self, *, purpose: str, model: str, auth: str, usage: dict[str, Any] | None,
               sdk_cost_usd: float | None, run_id: str, extra_searches: int = 0,
               extra: dict[str, Any] | None = None) -> float:
        """Append one row; cost = max(SDK estimate, our estimate) to stay conservative. Returns the cost.

        `extra_searches`: WebSearch calls seen in the tool trace but missing from `usage` (they run in a
        sub-request); each adds $0.01 to our estimate."""
        ours = round(estimate_cost(model, usage) + extra_searches * WEB_SEARCH_USD, 8)
        cost = max(ours, sdk_cost_usd or 0.0)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "purpose": purpose,
            "model": model,
            "auth": auth,
            "cost_usd": round(cost, 8),
            "sdk_cost_usd": sdk_cost_usd,
            "our_estimate_usd": ours,
            "input_tokens": (usage or {}).get("input_tokens"),
            "output_tokens": (usage or {}).get("output_tokens"),
            "cache_read_tokens": (usage or {}).get("cache_read_input_tokens"),
            "cache_write_tokens": (usage or {}).get("cache_creation_input_tokens"),
            "web_searches": ((usage or {}).get("server_tool_use") or {}).get("web_search_requests"),
            **(extra or {}),
        }
        with self._lock, file_lock(self.path), self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row) + "\n")                       # one locked write per row: no cross-process interleaving
        return cost
