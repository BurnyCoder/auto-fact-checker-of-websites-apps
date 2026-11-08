"""Timestamped logging, plus a full, uncut record of every LLM prompt, tool call and output.

Local context: `setup_logging` configures console + file logs; `llm_event` appends one JSON line per
LLM-related event to logs/llm-<timestamp>.jsonl and also prints it in full to the terminal.
Global context: the agent wrapper (pipeline/agent.py) calls `llm_event` for every prompt, assistant
block, tool call, tool result and final result, so a run can be audited afterwards. Secrets that look
like Anthropic keys are redacted everywhere.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")   # Anthropic API key / OAuth token shapes
_lock = threading.Lock()                               # the JSONL file is shared by async tasks/threads
_llm_log_path: Path | None = None
_echo_to_terminal = True


def redact(text: str) -> str:
    """Replace anything that looks like an Anthropic secret."""
    return _KEY_PATTERN.sub("sk-ant-***REDACTED***", text)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(logs_dir: Path, name: str = "factcheck", echo_llm: bool = True) -> Path:
    """Configure root logging (console + logs/<name>-<ts>.log) and the LLM JSONL file; return its path."""
    global _llm_log_path, _echo_to_terminal
    for stream in (sys.stdout, sys.stderr):              # Windows consoles/pipes default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = _RedactingFormatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):                 # idempotent: drop handlers from a previous call
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file_handler = logging.FileHandler(logs_dir / f"{name}-{stamp}.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _llm_log_path = logs_dir / f"llm-{name}-{stamp}.jsonl"
    _echo_to_terminal = echo_llm
    return _llm_log_path


def llm_event(kind: str, run_id: str, **fields: Any) -> None:
    """Record one LLM event (prompt, assistant text, tool_use, tool_result, result) in full."""
    record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind, "run_id": run_id, **fields}
    line = redact(json.dumps(record, ensure_ascii=False, default=str))
    with _lock:
        if _llm_log_path is not None:
            with _llm_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if _echo_to_terminal:
            print(f"[llm {record['ts']}] {line}", flush=True)
