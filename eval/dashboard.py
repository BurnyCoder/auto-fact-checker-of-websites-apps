"""Serve the live eval dashboard: `uv run python -m eval.dashboard` then open http://127.0.0.1:8770/dashboard.html

Local context: a stdlib static file server rooted at results/, with dashboard.html served from eval/.
Global context: eval/run.py rewrites results/<experiment>/progress.json after every item.
"""

from __future__ import annotations

import shutil
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(port: int = 8770) -> None:
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    shutil.copy(Path(__file__).with_name("dashboard.html"), results / "dashboard.html")
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(SimpleHTTPRequestHandler, directory=str(results)))
    print(f"dashboard: http://127.0.0.1:{port}/dashboard.html  (Ctrl+C to stop)")
    server.serve_forever()


if __name__ == "__main__":
    main()
