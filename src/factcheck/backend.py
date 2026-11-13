"""Backend thread: one asyncio event loop running the FastAPI server, the fact-check service and capture.

Local context: `Backend.start()` spins up a daemon thread with its own (Proactor) event loop, starts the
service workers and `uvicorn.Server.serve()` inside it, and returns once the server is listening.
Global context: Qt must own the main thread, and qasync/QtAsyncio break subprocesses on Windows
(https://bugreports-test.qt.io/browse/PYSIDE-769) while the Agent SDK spawns one CLI subprocess per query,
so all async work lives here. The UI talks to it with `Backend.call()` (asyncio.run_coroutine_threadsafe)
and receives events through listener callbacks (the UI wraps them in Qt signals, which are thread-safe).
uvicorn only installs signal handlers on the main thread, so `serve()` is safe in a worker thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import uvicorn

from factcheck.config import Settings
from factcheck.pipeline.agent import AgentRunner
from factcheck.pipeline.budget import BudgetLedger
from factcheck.pipeline.cache import VerdictCache
from factcheck.pipeline.fake import FakeRunner
from factcheck.pipeline.service import FactCheckService
from factcheck.server.api import create_app

log = logging.getLogger(__name__)


class Backend:
    def __init__(self, settings: Settings, runner: Any | None = None, port: int | None = None,
                 allowed_origins: set[str] | None = None):
        self.settings = settings
        self.ledger = BudgetLedger(settings.ledger_path, settings.total_budget_usd, settings.daily_app_budget_usd)
        self.runner = runner or (FakeRunner(settings) if settings.factcheck_fake_llm else AgentRunner(settings, self.ledger))
        self.service = FactCheckService(settings, self.runner, VerdictCache(settings.data_dir / "verdicts.sqlite"))
        self.app = create_app(settings, self.service, allowed_origins)
        self.port = port if port is not None else settings.server_port
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    ws_ping_interval=20, lifespan="off"))
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_tasks: list[Callable[[], Coroutine]] = []

    @property
    def extension_connected(self) -> bool:
        return self.app.state.hub.extension_connected

    def add_startup_task(self, factory: Callable[[], Coroutine]) -> None:
        """Register a coroutine (e.g. the capture loop) to run inside the backend loop once started."""
        self._startup_tasks.append(factory)

    def start(self, timeout: float = 20.0) -> None:
        self._thread = threading.Thread(target=self._run, name="factcheck-backend", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"backend did not start within {timeout}s (is port {self.port} free?)")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()            # ProactorEventLoop on Windows: supports subprocesses
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self.service.start()
        # keep references (the loop holds tasks weakly: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)
        # and cancel them on shutdown, or the capture loop is "destroyed but pending" when the app quits
        startup = [asyncio.create_task(factory()) for factory in self._startup_tasks]
        serve = asyncio.create_task(self.server.serve())
        while not self.server.started and not serve.done():
            await asyncio.sleep(0.05)
        if serve.done() and serve.exception():
            log.error("server failed: %s", serve.exception())
        self._ready.set()
        log.info("backend listening on http://127.0.0.1:%d (fake=%s)", self.port, self.settings.factcheck_fake_llm)
        try:
            await serve
        finally:
            for task in startup:
                task.cancel()
            await asyncio.gather(*startup, return_exceptions=True)
            await self.service.stop()

    def call(self, coro: Coroutine) -> concurrent.futures.Future:
        """Run a coroutine on the backend loop from another thread (e.g. the Qt UI)."""
        assert self.loop is not None, "backend not started"
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call_soon(self, fn: Callable, *args) -> None:
        assert self.loop is not None
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        if self._thread:
            self._thread.join(timeout)
