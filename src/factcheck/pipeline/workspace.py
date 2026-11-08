"""Per-claim `/work` folder: the only part of the PC the verification agent can see or write.

Local context: create a fresh folder for one claim, translate its Windows path to the WSL path that
bubblewrap bind-mounts as `/work`, and delete it afterwards.
Global context: `sandbox.run_bash` mounts this folder read-write and `fetchfile.fetch_file` saves
downloads into it, so the agent can download data in one tool call and analyse it in the next.
"""

from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterator


def to_wsl_path(path: Path) -> str:
    """Translate an absolute Windows path (C:\\a\\b) to WSL's default automount path (/mnt/c/a/b).

    WSL mounts each Windows drive at /mnt/<lowercase letter> by default
    (https://learn.microsoft.com/en-us/windows/wsl/wsl-config#automount-settings).
    """
    win = PureWindowsPath(path.resolve())              # absolute, normalised Windows path
    drive = win.drive.rstrip(":").lower()              # "C:" -> "c"
    if not re.fullmatch(r"[a-z]", drive):              # UNC paths (\\server\share) are not supported
        raise ValueError(f"cannot map {path} to a WSL path (need a drive-letter path)")
    rest = "/".join(win.parts[1:])                     # parts after the drive root, joined with "/"
    return f"/mnt/{drive}/{rest}"


def safe_name(text: str, limit: int = 40) -> str:
    """Reduce arbitrary text (a claim id, a filename) to a short filesystem-safe token."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")  # keep only portable characters
    return cleaned[:limit] or "x"                      # never return an empty name


@dataclass(frozen=True)
class Workspace:
    """A created per-claim folder, known by both its Windows and WSL paths."""

    host_path: Path      # e.g. C:\...\.agent-work\claim-1a2b
    wsl_path: str        # e.g. /mnt/c/.../.agent-work/claim-1a2b (bind-mounted as /work)


def create_workspace(root: Path, label: str) -> Workspace:
    """Create a new empty folder under `root`, unique even if `label` repeats."""
    folder = root / f"{safe_name(label)}-{uuid.uuid4().hex[:8]}"  # random suffix avoids collisions
    folder.mkdir(parents=True, exist_ok=False)         # fail loudly rather than reuse old contents
    return Workspace(host_path=folder, wsl_path=to_wsl_path(folder))


def remove_workspace(ws: Workspace) -> None:
    """Delete the folder and everything the agent put in it (ignore if already gone)."""
    shutil.rmtree(ws.host_path, ignore_errors=True)


@contextmanager
def workspace(root: Path, label: str, keep: bool = False) -> Iterator[Workspace]:
    """Context manager: a fresh workspace for one verification, removed on exit unless `keep`."""
    ws = create_workspace(root, label)
    try:
        yield ws
    finally:
        if not keep:                                   # KEEP_AGENT_WORK=true keeps it for debugging
            remove_workspace(ws)
