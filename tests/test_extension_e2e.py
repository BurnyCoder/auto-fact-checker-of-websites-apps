"""End-to-end: the real unpacked extension in Chromium talking to the real backend in fake-LLM mode ($0).

Global context: proves the full browser path — content script -> service worker -> authenticated
WebSocket -> FactCheckService -> back to the page as CSS highlights and a hover card — works without
spending budget. Extensions in Playwright: https://playwright.dev/python/docs/chrome-extensions
"""

from __future__ import annotations

import socket
import threading
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
def extension_page(tmp_path, backend, page_server):
    """Chromium with the unpacked extension, configured for `backend`, showing the fixture article."""
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        try:
            worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
            assert worker.url.startswith("chrome-extension://")
            worker.evaluate("s => chrome.storage.local.set(s)",
                            {"token": backend.settings.server_token, "autoMode": True, "debugExposeCard": True,
                             "serverUrl": f"ws://127.0.0.1:{backend.port}/ws"})
            page = ctx.new_page()
            page.goto(f"{page_server}/article.html")
            yield page
        finally:
            ctx.close()


def hover_card(page) -> str:
    """Hover the "330 metres" claim and read the card text (test mirror of the closed shadow root)."""
    box = page.evaluate("""() => { const b = document.querySelector('p b'); const r = b.getBoundingClientRect();
                                   return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }""")
    page.mouse.move(box["x"], box["y"])
    page.wait_for_function("() => !!document.querySelector('#fc-card-host').dataset.debugCard", timeout=5000)
    return page.evaluate("() => document.querySelector('#fc-card-host').dataset.debugCard")


def test_extension_highlights_and_hover_card(tmp_path, backend, page_server):
    with extension_page(tmp_path, backend, page_server) as page:
        # content script submits visible blocks after ~1.2 s; fake verdicts arrive right after
        page.wait_for_function(
            "() => ['fc-supported','fc-refuted','fc-nee','fc-conflicting']"
            ".reduce((n, k) => n + (CSS.highlights.get(k)?.size || 0), 0) >= 3", timeout=20000)
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
    with extension_page(tmp_path, backend, page_server) as page:
        page.wait_for_function("() => (CSS.highlights.get('fc-pending')?.size || 0) >= 3", timeout=20000)
        card = hover_card(page)
        assert "Couldn't check" in card and "DAILY_APP_BUDGET_USD" in card and "Try again" in card
