"""Run the real extension + real backend (Haiku) on a live page and save screenshots for the docs.

Usage:  uv run python scripts/demo_extension.py https://en.wikipedia.org/wiki/Eiffel_Tower --verdicts 3
Global context: this is the "run it like a user" check for the browser path. It spends real
(API-equivalent) budget through the normal app ledger purposes (app-extract / app-verify).
"""

from __future__ import annotations

import argparse
import socket
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from factcheck.backend import Backend
from factcheck.config import load_settings
from factcheck.log import setup_logging

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--verdicts", type=int, default=3, help="stop after this many verdicts")
    ap.add_argument("--out", default=str(ROOT / "docs" / "images" / "extension-demo.png"))
    ap.add_argument("--timeout", type=float, default=420)
    args = ap.parse_args()

    settings = load_settings()
    setup_logging(settings.logs_dir, name="demo-extension")
    backend = Backend(settings, port=free_port())
    verdicts: list[dict] = []
    backend.service.listeners.append(lambda e: verdicts.append(e) if e["type"] == "verdict" else None)
    backend.start()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), channel="chromium", headless=True, viewport={"width": 1280, "height": 900},
            args=[f"--disable-extensions-except={ROOT / 'extension'}", f"--load-extension={ROOT / 'extension'}"])
        worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
        worker.evaluate("s => chrome.storage.local.set(s)", {"token": settings.server_token, "autoMode": True,
                                                              "serverUrl": f"ws://127.0.0.1:{backend.port}/ws"})
        page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        start = time.time()
        while len(verdicts) < args.verdicts and time.time() - start < args.timeout:
            time.sleep(1)
        page.wait_for_timeout(800)
        print(f"{len(verdicts)} verdicts in {time.time() - start:.0f}s")
        for e in verdicts:
            v = e["verdict"]
            print(f"- {v.label} {v.confidence:.2f} | {v.claim} | flags={v.flags} | ${v.cost_usd:.4f}")
        # hover the first verdict's highlighted text to open the card, then screenshot
        rect = page.evaluate("""() => {
            for (const k of ['fc-refuted','fc-conflicting','fc-supported','fc-nee']) {
              const h = CSS.highlights.get(k); if (!h || !h.size) continue;
              const r = [...h][0].getBoundingClientRect(); r && window.scrollBy(0, r.top - 200);
              const r2 = [...h][0].getBoundingClientRect(); return {x: r2.left + Math.min(40, r2.width / 2), y: r2.top + r2.height / 2};
            } return null; }""")
        if rect:
            page.mouse.move(rect["x"], rect["y"])
            page.wait_for_timeout(700)
        page.screenshot(path=args.out)
        print("screenshot:", args.out)
        ctx.close()
    backend.stop()
    print(f"ledger total: ${backend.ledger.spent_total():.4f}")


if __name__ == "__main__":
    main()
