"""End-to-end: the real unpacked extension in Chromium talking to the real backend in fake-LLM mode ($0).

Global context: proves the full browser path — content script -> service worker -> authenticated
WebSocket -> FactCheckService -> back to the page as CSS highlights and a hover card — works without
spending budget. Extensions in Playwright: https://playwright.dev/python/docs/chrome-extensions
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from factcheck.backend import Backend
from factcheck.pipeline.budget import BudgetExceeded

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
FIXTURES = Path(__file__).parent / "fixtures"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def page_server():
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def backend(settings):
    settings.factcheck_fake_llm = True
    settings.max_auto_checks_per_batch = 10
    b = Backend(settings, port=free_port())                 # allowed origin = our extension's real ID
    b.start()
    yield b
    b.stop()


@contextmanager
def extension_page(tmp_path, server_url: str, token: str, page_url: str, auto: bool | None = True, debug_port=None):
    """Chromium with the unpacked extension configured for `server_url`, showing `page_url`.
    `auto=None` leaves autoMode unset, i.e. the extension's own default. Yields (worker, page)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    args = [f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"]
    if debug_port:
        args.append(f"--remote-debugging-port={debug_port}")
    with sync_api.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(tmp_path / "profile"), channel="chromium", headless=True, args=args)
        try:
            worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
            assert worker.url.startswith("chrome-extension://")
            # wait for the one-time settings migration in onInstalled, or it could overwrite what we set
            worker.evaluate("""async () => { for (let i = 0; i < 100; i++) {
                if ((await chrome.storage.local.get('settingsVersion')).settingsVersion === 2) return;
                await new Promise((r) => setTimeout(r, 50)); } throw new Error('settings migration did not run'); }""")
            settings = {"token": token, "debugExposeCard": True, "serverUrl": server_url}
            if auto is not None:
                settings["autoMode"] = auto
            worker.evaluate("s => chrome.storage.local.set(s)", settings)
            page = ctx.new_page()
            page.goto(page_url)
            yield worker, page
        finally:
            ctx.close()


def backend_page(tmp_path, backend, page_server, **kw):
    return extension_page(tmp_path, f"ws://127.0.0.1:{backend.port}/ws", backend.settings.server_token,
                          f"{page_server}/article.html", **kw)


def press_check(worker, page) -> None:
    """What the toolbar button and Alt+Shift+F do: the service worker asks the active tab to check the page."""
    worker.evaluate("""async (url) => { const [tab] = await chrome.tabs.query({ url });
                                         await askTab(tab.id, { type: "checkPage" }); }""", page.url)


VERDICTS = ("() => ['fc-supported','fc-refuted','fc-nee','fc-conflicting']"
            ".reduce((n, k) => n + (CSS.highlights.get(k)?.size || 0), 0)")
TOAST = "() => document.querySelector('#fc-card-host')?.dataset.debugToast || ''"


def hover_card(page) -> str:
    """Hover the "330 metres" claim and read the card text (test mirror of the closed shadow root)."""
    box = page.evaluate("""() => { const b = document.querySelector('p b'); const r = b.getBoundingClientRect();
                                   return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""")
    page.mouse.move(box["x"], box["y"])
    page.wait_for_function("() => !!document.querySelector('#fc-card-host').dataset.debugCard", timeout=5000)
    return page.evaluate("() => document.querySelector('#fc-card-host').dataset.debugCard")


def test_extension_highlights_and_hover_card(tmp_path, backend, page_server):
    with backend_page(tmp_path, backend, page_server) as (_, page):
        # auto mode: the content script submits visible blocks after ~1.2 s; fake verdicts arrive right after
        page.wait_for_function(f"({VERDICTS})() >= 3", timeout=20000)
        assert backend.extension_connected
        # navigation text must not be checked
        assert page.evaluate("() => [...CSS.highlights.values()].every(h => [...h].every(r => !r.startContainer.parentElement.closest('nav')))")
        assert page.evaluate("() => document.querySelector('#fc-card-host').shadowRoot") is None   # closed to the page
        card = hover_card(page)
        assert "Why this confidence" in card and "62%" in card and "Eiffel Tower" in card


def test_failed_check_explains_why_in_the_card(tmp_path, backend, page_server, monkeypatch):
    """Regression (2026-11-13, live Chrome test): a refused check looked exactly like "Not checked yet"."""
    async def refuse(*args, **kwargs):
        raise BudgetExceeded("daily app budget $1.00 reached ($0.83 spent, a call may cost up to $0.21); "
                             "raise DAILY_APP_BUDGET_USD in .env to continue")

    monkeypatch.setattr("factcheck.pipeline.service.verify_claim", refuse)
    with backend_page(tmp_path, backend, page_server) as (_, page):
        page.wait_for_function("() => (CSS.highlights.get('fc-pending')?.size || 0) >= 3", timeout=20000)
        card = hover_card(page)
        assert "Couldn't check" in card and "DAILY_APP_BUDGET_USD" in card and "Try again" in card


def test_default_is_manual_nothing_sent_until_asked(tmp_path, backend, page_server):
    """User decision (2026-11-13): check only when the toolbar button / keyboard shortcut is pressed."""
    with backend_page(tmp_path, backend, page_server, auto=None) as (worker, page):
        page.wait_for_timeout(3000)
        assert page.evaluate(VERDICTS) == 0 and not backend.extension_connected   # no text left the page
        press_check(worker, page)
        page.wait_for_function(f"({VERDICTS})() >= 3", timeout=20000)


class CdpErrors:
    """Errors Chrome would list in chrome://extensions, read over CDP: console errors, uncaught exceptions and
    browser log entries (a failed WebSocket handshake arrives as Log.entryAdded, source "network")."""

    def __init__(self, debug_port: int):
        self.port, self.events, self.stop, self.watched = debug_port, [], threading.Event(), set()

    def watch(self, kind: str, url_part: str = "") -> None:
        for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list")):
            if t["type"] == kind and url_part in t["url"] and t["id"] not in self.watched:
                self.watched.add(t["id"])
                threading.Thread(target=self._run, args=(t["webSocketDebuggerUrl"], kind), daemon=True).start()
        time.sleep(0.3)                                  # let Runtime/Log.enable land before acting

    def _run(self, ws_url: str, kind: str) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        with connect(ws_url, max_size=None) as ws:
            for i, method in enumerate(("Runtime.enable", "Log.enable")):
                ws.send(json.dumps({"id": i + 1, "method": method}))
            while not self.stop.is_set():
                try:
                    msg = json.loads(ws.recv(timeout=0.3))
                except TimeoutError:
                    continue
                except ConnectionClosed:
                    return
                m, prm = msg.get("method"), msg.get("params", {})
                if m == "Runtime.exceptionThrown":
                    self.events.append((kind, prm["exceptionDetails"].get("exception", {}).get("description", "")))
                elif m == "Runtime.consoleAPICalled" and prm["type"] == "error":
                    self.events.append((kind, str(prm["args"])))
                elif m == "Log.entryAdded" and prm["entry"]["level"] == "error" and kind == "service_worker":
                    # (page network entries are the test page's own, e.g. its missing favicon.ico)
                    self.events.append((kind, prm["entry"]["text"]))


def test_no_extension_errors_when_app_is_down_or_extension_reloaded(tmp_path, page_server):
    """Regression (2026-11-13, user report "extension had errors"): with the app down the worker logged
    net::ERR_CONNECTION_REFUSED on every reconnect, and after a reload the old content script threw
    "Extension context invalidated". (Playwright's command-line extension does not come back after
    chrome.runtime.reload(), so re-injection into the old tab is covered by activeTab, not by this test.)"""
    dbg, dead = free_port(), free_port()                 # nothing listens on `dead`: the app is not running
    errors = CdpErrors(dbg)
    with extension_page(tmp_path, f"ws://127.0.0.1:{dead}/ws", "t" * 20, f"{page_server}/article.html",
                        auto=None, debug_port=dbg) as (worker, page):
        errors.watch("service_worker")
        errors.watch("page", "article.html")
        page.wait_for_timeout(2000)
        press_check(worker, page)                        # app down: one quiet probe, then a clear message
        page.wait_for_function(f"({TOAST})().includes('not running')", timeout=10000)
        worker.evaluate("() => { chrome.runtime.reload(); }")   # = reload / remove + Load unpacked
        page.wait_for_timeout(2000)
        page.mouse.move(50, 50)
        page.mouse.move(300, 200)
        assert page.evaluate("() => !document.querySelector('#fc-card-host')")   # the orphan removed its UI
        page.wait_for_timeout(500)
    errors.stop.set()
    assert errors.events == []
