"""FactCheckService orchestration tests with the deterministic FakeRunner ($0)."""

from __future__ import annotations

import asyncio

from factcheck.models import SourceInfo, TextBlock
from factcheck.pipeline.cache import VerdictCache
from factcheck.pipeline.fake import FakeRunner
from factcheck.pipeline.service import FactCheckService

TEXT = ("The tower is 330 m tall. It opened in 1889. About 7 million people visit each year. "
        "Paris has 2.1 million residents. I like it.")


async def wait_for(events, kind, n, timeout=10.0):
    async with asyncio.timeout(timeout):
        while sum(1 for e in events if e["type"] == kind) < n:
            await asyncio.sleep(0.02)


async def make_service(settings, cache=True):
    settings.max_auto_checks_per_batch = 3
    settings.app_concurrency = 2
    svc = FactCheckService(settings, FakeRunner(settings),
                           VerdictCache(settings.data_dir / "cache.sqlite") if cache else None)
    events: list[dict] = []
    svc.listeners.append(events.append)
    svc.start()
    return svc, events


async def test_submit_auto_checks_first_three_then_waits_for_click(settings):
    svc, events = await make_service(settings)
    src = SourceInfo(kind="chrome", source_id="tab1")
    claims = await svc.submit(src, [TextBlock(id="b1", text=TEXT)])
    assert len(claims) == 4                                     # four sentences with numbers (fake extractor)
    assert [c.status for c in claims].count("pending") == 1
    await wait_for(events, "verdict", 3)
    pending = next(c for c in claims if c.status == "pending")
    assert svc.check(pending.id)
    await wait_for(events, "verdict", 4)
    await svc.stop()


async def test_same_blocks_not_reextracted_and_cache_reused(settings):
    svc, events = await make_service(settings)
    src = SourceInfo(kind="chrome", source_id="tab1")
    await svc.submit(src, [TextBlock(id="b1", text="The tower is 330 m tall.")])
    await wait_for(events, "verdict", 1)
    assert await svc.submit(src, [TextBlock(id="b1", text="The tower is 330 m tall.")]) == []   # unchanged block skipped
    other = SourceInfo(kind="desktop", source_id="win9")
    claims = await svc.submit(other, [TextBlock(id="x", text="The tower is 330 m tall.")])     # same claim elsewhere
    await wait_for(events, "verdict", 2)
    last = [e for e in events if e["type"] == "verdict"][-1]["verdict"]
    assert "cached" in last.flags and claims[0].status == "done"
    await svc.stop()


async def test_cancel_source_skips_queued_work(settings):
    settings.max_auto_checks_per_batch = 10
    svc, events = await make_service(settings, cache=False)
    gate = asyncio.Event()
    original = svc.runner.run

    async def slow_run(prompt, **kw):                         # verifications block until released
        if kw.get("kind") == "verify":
            await gate.wait()
        return await original(prompt, **kw)

    svc.runner.run = slow_run
    src = SourceInfo(kind="chrome", source_id="tabX")
    await svc.submit(src, [TextBlock(id="b", text=TEXT)])
    svc.cancel_source("tabX")
    gate.set()
    await asyncio.sleep(0.3)
    assert not [e for e in events if e["type"] == "verdict"]  # running ones cancelled, queued ones skipped
    await svc.stop()
