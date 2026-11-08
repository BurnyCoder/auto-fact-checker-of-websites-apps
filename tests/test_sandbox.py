"""Security and toolchain tests for the WSL bubblewrap sandbox (real WSL; skipped if unavailable).

Global context: the sandbox is the boundary between untrusted screen text and the user's PC, so these
tests try the escapes a prompt-injected model might attempt: network, Windows files, the WSL home,
writing outside /work, runaway CPU, fork bombs and memory bombs.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from factcheck.pipeline.sandbox import SandboxConfig, run_bash, sandbox_available, trim
from factcheck.pipeline.workspace import workspace

OK, WHY = sandbox_available()
pytestmark = pytest.mark.skipif(not OK, reason=f"sandbox unavailable: {WHY}")


@pytest.fixture
def ws(tmp_path):
    """A throwaway workspace under pytest's temp dir (lives on C:, so it maps to /mnt/c/...)."""
    with workspace(tmp_path, "test") as w:
        yield w


async def test_python_known_answer(ws):
    res = await run_bash("python -c 'print(2**10)'", ws)
    assert res.exit_code == 0 and res.stdout.strip() == "1024"


async def test_toolchain_known_answers(ws):
    """Every advertised language runs: venv libs, node, gcc-compiled C, bc, deno, awk, jq, sqlite3."""
    script = r"""
set -e
python -c 'import numpy, pandas, scipy, sympy, statsmodels, pint, pypdf, bs4, openpyxl; print("py", numpy.arange(4).sum())'
node -e 'console.log("node", 6*7)'
printf '#include <stdio.h>\nint main(){printf("c %d\\n", 6*7);return 0;}' > t.c && gcc t.c -o t && ./t
echo "bc $(echo '6*7' | bc)"
deno eval 'console.log("deno", 6*7)'
echo 'awk' | awk '{print $1, 6*7}'
echo '{"a":42}' | jq -r '"jq \(.a)"'
sqlite3 :memory: 'select "sqlite", 6*7;'
"""
    res = await run_bash(script, ws)
    assert res.exit_code == 0, res.as_tool_text()
    for expected in ("py 6", "node 42", "c 42", "bc 42", "deno 42", "awk 42", "jq 42", "sqlite|42"):
        assert expected in res.stdout, (expected, res.as_tool_text())


async def test_no_network(ws):
    """Neither curl nor a raw Python socket can reach the internet."""
    res = await run_bash(
        "curl -sS -m 5 https://example.com >/dev/null && echo CURL-OK; "
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=5); print('SOCK-OK')\"",
        ws,
    )
    assert "CURL-OK" not in res.stdout and "SOCK-OK" not in res.stdout, res.as_tool_text()


async def test_host_files_invisible(ws):
    """Windows drives, the real WSL home and Claude credentials do not exist inside."""
    res = await run_bash("ls /mnt; ls /home; ls ~/.claude; cat /etc/passwd; echo HOME=$HOME", ws)
    assert "No such file" in res.stderr                     # /mnt and /etc/passwd are absent
    assert "burny" not in res.stdout and "Users" not in res.stdout, res.as_tool_text()
    assert "HOME=/home/agent" in res.stdout


async def test_writes_only_in_work_and_tmp(ws):
    res = await run_bash("touch /usr/evil 2>&1; touch /tools/evil 2>&1; touch /tmp/ok && touch /work/ok && echo WROTE", ws)
    assert "Read-only file system" in res.stdout
    assert "WROTE" in res.stdout
    assert (ws.host_path / "ok").exists()                   # /work really is the Windows-side folder


async def test_work_persists_across_calls_but_tmp_does_not(ws):
    await run_bash("echo 42 > /work/data.txt; echo gone > /tmp/x", ws)
    res = await run_bash("cat /work/data.txt; cat /tmp/x 2>&1", ws)
    assert "42" in res.stdout and "No such file" in res.stdout


async def test_infinite_loop_is_killed(ws):
    cfg = replace(SandboxConfig(), timeout_s=3)
    res = await run_bash("while true; do :; done", ws, cfg)
    assert res.timed_out and res.duration_s < 25


async def test_fork_bomb_is_contained(ws):
    cfg = replace(SandboxConfig(), timeout_s=5, nproc=64)
    res = await run_bash(":(){ :|:& };:", ws, cfg)
    assert res.duration_s < 30                              # returns instead of freezing WSL


async def test_memory_bomb_fails(ws):
    res = await run_bash("python -c \"x = bytearray(4 * 1024**3); print('ALLOCATED')\"", ws)
    assert "ALLOCATED" not in res.stdout and "MemoryError" in res.stderr, res.as_tool_text()


def test_trim_keeps_head_and_tail():
    text = "A" * 50 + "B" * 50
    out = trim(text, 20)
    assert out.startswith("A" * 10) and out.endswith("B" * 10) and "omitted" in out
