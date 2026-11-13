"""FactCheckService: the app's orchestrator from captured blocks to verdict events.

Local context: de-duplicates blocks per source, extracts claims, answers from the cache when it can,
auto-checks the first few new claims of each batch (the rest wait for a click), runs verifications
newest-first on a fixed number of workers, and cancels work for pages/windows that went away.
Global context: the server (Chrome extension) and the desktop capture loop call `submit()`; the UI and
the extension receive plain-dict events through listeners:
  {"type": "claims",  "source_id", "claims": [Claim...]}
  {"type": "verdict", "source_id", "claim_id", "verdict": Verdict}
  {"type": "status",  "source_id", "claim_id", "status": "checking" | "error", "message"}
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable

from factcheck.config import Settings
from factcheck.models import Claim, SourceInfo, TextBlock
from factcheck.pipeline.budget import BudgetExceeded
from factcheck.pipeline.cache import VerdictCache
from factcheck.pipeline.extract import extract_claims
from factcheck.pipeline.prompts import context_lines
from factcheck.pipeline.segment import fingerprint
from factcheck.pipeline.verify import VerifyConfig, verify_claim

log = logging.getLogger(__name__)
Listener = Callable[[dict], None]


class FactCheckService:
    def __init__(self, settings: Settings, runner, cache: VerdictCache | None = None):
        self.settings = settings
        self.runner = runner
        self.cache = cache
        self.listeners: list[Listener] = []
        self.claims: dict[str, Claim] = {}               # claim_id -> Claim (latest known)
        self._seen_blocks: dict[str, set[str]] = {}      # source_id -> block fingerprints already extracted
        self._queue: asyncio.PriorityQueue | None = None
        self._seq = itertools.count()
        self._running: dict[str, asyncio.Task] = {}      # claim_id -> running verification task
        self._inflight: set[str] = set()                 # claim ids queued or running (prevents duplicate checks)
        self._cancelled: set[str] = set()                # source ids whose queued work must be skipped
        self._workers: list[asyncio.Task] = []
        self.verify_cfg = VerifyConfig(max_searches=settings.max_web_searches, max_fetches=settings.max_web_fetches,
                                       max_turns=settings.max_turns, keep_work=settings.keep_agent_work,
                                       timeout_s=settings.verify_timeout_s,
                                       verify_quotes=not settings.factcheck_fake_llm)  # fake sources need no re-fetch

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the worker pool (call from inside the running event loop)."""
        self._queue = asyncio.PriorityQueue()
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(self.settings.app_concurrency)]

    async def stop(self) -> None:
        for task in [*self._workers, *self._running.values()]:
            task.cancel()
        await asyncio.gather(*self._workers, *self._running.values(), return_exceptions=True)

    def emit(self, event: dict) -> None:
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                log.exception("listener failed")

    # ------------------------------------------------------------------ API
    async def submit(self, source: SourceInfo, blocks: list[TextBlock], auto: bool = True,
                     force: bool = False) -> list[Claim]:
        """Extract claims from new blocks of `source`, emit them, and queue verifications.

        `force=True` (hotkey / manual check) re-extracts blocks even if they were seen before.
        """
        self._cancelled.discard(source.source_id)
        seen = self._seen_blocks.setdefault(source.source_id, set())
        fresh = [b for b in blocks if b.text.strip() and (force or fingerprint(b.text) not in seen)]
        seen.update(fingerprint(b.text) for b in fresh)
        if not fresh:
            return []
        try:
            claims = await extract_claims(fresh, source, self.runner, purpose="app-extract")
        except BudgetExceeded as exc:
            log.warning("not extracting claims from %s: %s", source.source_id, exc)
            self.emit({"type": "status", "source_id": source.source_id, "claim_id": None, "status": "error",
                       "message": str(exc)})
            return []
        auto_left = self.settings.max_auto_checks_per_batch if auto else 0
        to_queue: list[Claim] = []
        cache_hits = []
        self._prune()
        for i, claim in enumerate(claims):
            known = self.claims.get(claim.id)
            if known is not None and (known.id in self._inflight or known.status == "done"):
                claims[i] = known                        # already checking / checked: never verify twice
                continue
            self.claims[claim.id] = claim
            cached = self.cache.get(claim.text) if self.cache else None
            if cached is not None:                       # reuse a recent verdict: no cost
                claim.status = "done"
                cached.claim_id, cached.claim = claim.id, claim.text
                cached.flags = sorted(set(cached.flags) | {"cached"})
                cache_hits.append((claim, cached))
            elif auto_left > 0:                          # auto-check only the first few new claims
                auto_left -= 1
                claim.status = "checking"
                to_queue.append(claim)
            else:
                claim.status = "pending"                 # shown as "click to check"
        self.emit({"type": "claims", "source_id": source.source_id, "claims": claims})
        for claim, verdict in cache_hits:
            self.emit({"type": "verdict", "source_id": source.source_id, "claim_id": claim.id, "verdict": verdict})
        for claim in to_queue:
            self._enqueue(claim)
        return claims

    def check(self, claim_id: str) -> bool:
        """Queue a verification for a known claim (the 'click to check' path)."""
        claim = self.claims.get(claim_id)
        if claim is None or claim_id in self._inflight or claim.status == "done":
            return False
        claim.status = "checking"
        self._cancelled.discard(claim.source.source_id)
        self._enqueue(claim)
        return True

    def cancel_source(self, source_id: str) -> None:
        """Drop queued and running work for a page/window that is gone."""
        self._cancelled.add(source_id)
        for cid, task in list(self._running.items()):
            claim = self.claims.get(cid)
            if claim is not None and claim.source.source_id == source_id:
                task.cancel()
        for claim in self.claims.values():                # queued-but-dropped claims become "click to check" again
            if claim.source.source_id == source_id and claim.id in self._inflight and claim.id not in self._running:
                self._inflight.discard(claim.id)
                self._set_pending(claim)

    # ------------------------------------------------------------------ internals
    def _set_pending(self, claim: Claim) -> None:
        claim.status = "pending"
        self.emit({"type": "status", "source_id": claim.source.source_id, "claim_id": claim.id, "status": "pending",
                   "message": "cancelled; click to check"})

    def _prune(self, keep: int = 5000) -> None:
        """Bound memory in long sessions: forget the oldest finished claims and per-source block fingerprints."""
        if len(self.claims) > keep:
            for cid in list(self.claims)[: len(self.claims) - keep]:
                if cid not in self._inflight:
                    self.claims.pop(cid, None)
        for seen in self._seen_blocks.values():
            if len(seen) > 4000:
                seen.clear()

    def _enqueue(self, claim: Claim) -> None:
        assert self._queue is not None, "call start() first"
        self._inflight.add(claim.id)
        self._queue.put_nowait((-next(self._seq), claim.id))   # negative counter = newest first
        self.emit({"type": "status", "source_id": claim.source.source_id, "claim_id": claim.id, "status": "checking",
                   "message": "queued"})

    async def _worker(self, n: int) -> None:
        assert self._queue is not None
        while True:
            _, claim_id = await self._queue.get()
            claim = self.claims.get(claim_id)
            if claim is None or claim.source.source_id in self._cancelled or claim_id not in self._inflight:
                continue
            task = asyncio.create_task(self._verify(claim))
            self._running[claim_id] = task
            try:
                await task
            except asyncio.CancelledError:
                # .cancelling() (not .cancelled(), always False while the task runs) tells whether the *worker*
                # is being stopped, as opposed to only this verification (code-review finding #3)
                if asyncio.current_task().cancelling():
                    raise
                self._set_pending(claim)
            except Exception:
                log.exception("verification of %s crashed", claim_id)
                claim.status = "error"
            finally:
                self._running.pop(claim_id, None)
                self._inflight.discard(claim_id)

    async def _verify(self, claim: Claim) -> None:
        context = context_lines(app=claim.source.app, title=claim.source.title, url=claim.source.url)
        try:
            verdict = await verify_claim(claim, self.runner, self.verify_cfg, purpose="app-verify", context=context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            claim.status = "error"
            if isinstance(exc, BudgetExceeded):
                log.warning("not checking %s: %s", claim.id, exc)
            else:
                log.exception("verify_claim failed for %s", claim.id)
            self.emit({"type": "status", "source_id": claim.source.source_id, "claim_id": claim.id, "status": "error",
                       "message": f"{type(exc).__name__}: {exc}"[:300]})
            return
        claim.status = "done" if not verdict.error else "error"
        if self.cache is not None:
            self.cache.put(claim.text, verdict)
        self.emit({"type": "verdict", "source_id": claim.source.source_id, "claim_id": claim.id, "verdict": verdict})
