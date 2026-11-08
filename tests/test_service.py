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
    assert all(c.status == "pending" for c in svc.claims.values())   # recoverable via "click to check"
    first = next(iter(svc.claims))
    assert svc.check(first)
    await wait_for(events, "verdict", 1)
    await svc.stop()


def slowed(svc, gate: asyncio.Event):
    original = svc.runner.run

    async def slow_run(prompt, **kw):
        if kw.get("kind") == "verify":
            await gate.wait()
        return await original(prompt, **kw)

    svc.runner.run = slow_run


async def test_stop_returns_while_a_verification_is_running(settings):
    svc, _ = await make_service(settings, cache=False)
    slowed(svc, asyncio.Event())
    await svc.submit(SourceInfo(kind="chrome", source_id="t"), [TextBlock(id="b", text="The tower is 330 m tall.")])
    await asyncio.sleep(0.2)
    async with asyncio.timeout(5):                        # used to hang forever (code-review finding #3)
        await svc.stop()


async def test_crashing_verification_does_not_kill_workers(settings):
    svc, events = await make_service(settings, cache=False)
    calls = {"n": 0}
    original = svc.runner.run

    async def flaky(prompt, **kw):
        if kw.get("kind") == "verify":
            calls["n"] += 1
            if calls["n"] <= 3:
                raise ValueError("Invalid IPv6 URL")      # e.g. a bracketed IPv6 URL in a search result
        return await original(prompt, **kw)

    svc.runner.run = flaky
    await svc.submit(SourceInfo(kind="chrome", source_id="t"), [TextBlock(id="b", text=TEXT)])
    async with asyncio.timeout(10):
        while sum(c.status == "error" for c in svc.claims.values()) < 3:
            await asyncio.sleep(0.05)
    pending = next(c for c in svc.claims.values() if c.status == "pending")
    assert svc.check(pending.id)                          # workers survived: the 4th claim still gets verified
    await wait_for(events, "verdict", 1)
    await svc.stop()


async def test_resubmitting_does_not_verify_twice(settings):
    svc, events = await make_service(settings, cache=False)
    gate = asyncio.Event()
    slowed(svc, gate)
    src = SourceInfo(kind="desktop", source_id="screen:1")
    await svc.submit(src, [TextBlock(id="b", text="The tower is 330 m tall.")], force=True)
    await svc.submit(src, [TextBlock(id="b", text="The tower is 330 m tall.")], force=True)
    gate.set()
    await wait_for(events, "verdict", 1)
    await asyncio.sleep(0.3)
    assert sum(e["type"] == "verdict" for e in events) == 1
    await svc.stop()
