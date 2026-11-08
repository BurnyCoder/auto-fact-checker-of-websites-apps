"""Sandboxed bash for the verification agent: bubblewrap inside WSL2, offline, one writable folder.

Local context: build the bubblewrap command line, pipe the agent's script to `bash -s` inside it,
enforce limits, and return trimmed output.
Global context: exposed to the agent as the `bash` tool (see agent_tools.py). Screen text is
untrusted, so a prompt injection could make the model write hostile code; this module is the
security boundary. It mirrors the isolation layer Claude Code itself uses on Linux/WSL2
(https://code.claude.com/docs/en/sandboxing) but with an explicit mount *allow*-list:
  read-only  /usr, /etc/alternatives, /etc/ld.so.cache  (system toolchain from apt: python3, node, gcc, ...)
  read-only  /tools                                     (venv + deno from scripts/setup_wsl_sandbox.sh)
  read-write /work                                      (this claim's workspace, see workspace.py)
  fresh tmpfs /tmp and /home/agent                      (wiped after every call)
Nothing else — not /mnt/c (Windows drives), not the WSL home, not Claude credentials — exists inside.
`--unshare-all` gives the sandbox its own empty network namespace, so it has no network at all.
"""

from __future__ import annotations

import asyncio
import functools
import shutil
import subprocess
import time
from dataclasses import dataclass

from factcheck.pipeline.workspace import Workspace


@dataclass(frozen=True)
class SandboxConfig:
    """Limits and locations for one sandboxed call (defaults match the approved plan)."""

    tools_dir: str = "~/projects/fact-check-sandbox/tools"  # WSL path; "~" is expanded at runtime
    timeout_s: int = 60                 # wall-clock limit, enforced by coreutils `timeout -s KILL`
    data_bytes: int = 2 * 1024**3       # RLIMIT_DATA: caps heap memory (RLIMIT_AS would break V8 in node/deno)
    nproc: int = 256                    # RLIMIT_NPROC: contains fork bombs
    fsize_bytes: int = 200 * 1024**2    # RLIMIT_FSIZE: largest file the agent can write
    max_output_chars: int = 8000        # output returned to the model (head + tail kept)


@dataclass(frozen=True)
class SandboxResult:
    """Outcome of one sandboxed script."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float

    def as_tool_text(self) -> str:
        """Render for the model: exit code first, then the two streams."""
        flag = " (killed: time limit)" if self.timed_out else ""
        return f"exit_code: {self.exit_code}{flag}\n--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"


def trim(text: str, limit: int) -> str:
    """Keep the head and tail of long output so the model sees both the start and the final result."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters omitted] ...\n{text[-half:]}"


def seccomp_program() -> bytes:
    """Classic-BPF seccomp filter (raw `struct sock_filter[]`, the format `bwrap --seccomp FD` reads).

    Why: `--unshare-all` removes TCP/IP but NOT AF_VSOCK; the code review (2026-11-08) showed a sandboxed process
    could connect to a vsock service on the Windows host, and WSL's interop (running Windows .exe files) also rides on
    host channels. This filter makes socket(AF_VSOCK, ...) fail with EAFNOSUPPORT, kills any non-x86-64 syscall ABI,
    and refuses x32-ABI syscall numbers (which could otherwise bypass the socket check).
    Layout of struct seccomp_data: nr @0, arch @4, args[0] @16 (https://man7.org/linux/man-pages/man2/seccomp.2.html).
    """
    import struct

    LD_W_ABS, JEQ_K, JGE_K, RET_K = 0x20, 0x15, 0x35, 0x06
    AUDIT_ARCH_X86_64, X32_BIT, NR_SOCKET, AF_VSOCK, EAFNOSUPPORT = 0xC000003E, 0x40000000, 41, 40, 97
    RET_ERRNO, RET_ALLOW, RET_KILL_PROCESS = 0x00050000, 0x7FFF0000, 0x80000000
    prog = [
        (LD_W_ABS, 0, 0, 4),                       # 0: A = arch
        (JEQ_K, 0, 7, AUDIT_ARCH_X86_64),          # 1: arch == x86_64 ? 2 : 9 (kill)
        (LD_W_ABS, 0, 0, 0),                       # 2: A = syscall nr
        (JGE_K, 5, 0, X32_BIT),                    # 3: nr >= x32 bit ? 9 (kill) : 4
        (JEQ_K, 0, 3, NR_SOCKET),                  # 4: nr == socket ? 5 : 8 (allow)
        (LD_W_ABS, 0, 0, 16),                      # 5: A = args[0] (domain, low 32 bits)
        (JEQ_K, 0, 1, AF_VSOCK),                   # 6: domain == AF_VSOCK ? 7 : 8
        (RET_K, 0, 0, RET_ERRNO | EAFNOSUPPORT),   # 7: fail the call
        (RET_K, 0, 0, RET_ALLOW),                  # 8: allow
        (RET_K, 0, 0, RET_KILL_PROCESS),           # 9: kill
    ]
    return b"".join(struct.pack("<HBBI", *ins) for ins in prog)


def seccomp_file() -> str:
    """Write the filter next to the project data and return its WSL path (read by bash outside the sandbox)."""
    from factcheck.config import PROJECT_ROOT
    from factcheck.pipeline.workspace import to_wsl_path

    path = PROJECT_ROOT / "data" / "sandbox-seccomp.bpf"
    path.parent.mkdir(exist_ok=True)
    data = seccomp_program()
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    return to_wsl_path(path)


@functools.lru_cache(maxsize=1)
def wsl_home() -> str:
    """The WSL user's home directory, asked once from WSL itself (e.g. /home/burny)."""
    out = subprocess.run(["wsl.exe", "-e", "sh", "-c", "printf %s \"$HOME\""], capture_output=True, timeout=30, check=False)
    return out.stdout.decode("utf-8").strip()


def resolve_tools_dir(cfg: SandboxConfig) -> str:
    """Expand a leading '~' in the configured tools folder to the real WSL home."""
    return cfg.tools_dir.replace("~", wsl_home(), 1) if cfg.tools_dir.startswith("~") else cfg.tools_dir


def build_command(cfg: SandboxConfig, work_wsl_path: str) -> list[str]:
    """Full argv, starting with wsl.exe, that runs `bash -s` (script on stdin) inside bubblewrap.

    bubblewrap flags: https://manpages.ubuntu.com/manpages/noble/man1/bwrap.1.html
    prlimit flags:    https://man7.org/linux/man-pages/man1/prlimit.1.html
    """
    tools = resolve_tools_dir(cfg)
    return [
        "wsl.exe", "-e",                                # run a Linux program directly (no login shell)
        # open the seccomp filter on fd 3 outside the sandbox, then exec bwrap with the remaining args verbatim
        "bash", "-c", 'exec 3<"$1"; shift; exec "$@"', "fc-sandbox", seccomp_file(),
        "bwrap",
        "--seccomp", "3",                               # deny AF_VSOCK sockets and non-x86-64 ABIs (see seccomp_program)
        "--unshare-all",                                # new user/pid/net/ipc/uts/cgroup namespaces -> no TCP/IP
        "--unshare-user",                               # explicit (--unshare-all only *tries*); required by --disable-userns
        "--disable-userns",                             # no nested user namespaces inside the sandbox
        "--die-with-parent",                            # sandbox dies if wsl.exe / our process dies
        "--new-session",                                # new terminal session: blocks TIOCSTI keystroke injection
        "--clearenv",                                   # start from an empty environment (no host secrets)
        "--ro-bind", "/usr", "/usr",                    # system toolchain, read-only
        "--symlink", "usr/bin", "/bin",                 # Ubuntu's merged-/usr layout
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--ro-bind", "/etc/alternatives", "/etc/alternatives",  # awk, cc, ... are alternatives symlinks
        "--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache",    # dynamic linker cache
        "--ro-bind", tools, "/tools",                   # venv + deno, read-only
        "--bind", work_wsl_path, "/work",               # this claim's workspace, read-write
        "--tmpfs", "/tmp",                              # scratch space, discarded after the call
        "--tmpfs", "/home/agent",                       # empty home for caches (npm, deno, pip)
        "--proc", "/proc",
        "--dev", "/dev",
        "--setenv", "HOME", "/home/agent",
        "--setenv", "PATH", "/tools/bin:/tools/venv/bin:/usr/local/bin:/usr/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "MPLBACKEND", "Agg",                # headless plotting if matplotlib is ever used
        "--chdir", "/work",                             # start where downloaded files are
        "--",
        "prlimit",
        f"--data={cfg.data_bytes}",
        f"--nproc={cfg.nproc}",
        f"--fsize={cfg.fsize_bytes}",
        "--",
        "timeout", "-s", "KILL", str(cfg.timeout_s),    # hard wall-clock kill of the whole script
        "bash", "-s",                                   # read the script from stdin (no quoting issues)
    ]


def sandbox_available() -> tuple[bool, str]:
    """Check that WSL, bubblewrap and the tools folder exist; return (ok, reason)."""
    if shutil.which("wsl.exe") is None:
        return False, "wsl.exe not found (WSL2 is required for the sandboxed bash tool)"
    try:
        tools = resolve_tools_dir(SandboxConfig())
        probe = subprocess.run(
            ["wsl.exe", "-e", "sh", "-c", f"command -v bwrap && test -x '{tools}/venv/bin/python' && echo ok"],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"WSL probe failed: {exc}"
    if b"ok" not in probe.stdout:
        return False, "bwrap or the sandbox toolchain is missing; run: wsl.exe -e bash scripts/setup_wsl_sandbox.sh"
    return True, "ok"


DEFAULT_SANDBOX = SandboxConfig()


async def run_bash(script: str, ws: Workspace, cfg: SandboxConfig = DEFAULT_SANDBOX) -> SandboxResult:
    """Run `script` with bash inside the sandbox, with `ws` mounted at /work.

    The outer asyncio timeout (limit + 20 s) is only a backstop for a hung wsl.exe; the inner
    `timeout -s KILL` is what normally stops runaway scripts.
    """
    argv = build_command(cfg, ws.wsl_path)
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(       # needs the Proactor event loop on Windows (the default)
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async def read_capped(stream, cap: int = 4 * 1024**2) -> bytes:
        """Read a pipe to EOF but keep only head + tail (review finding: `yes` for 60 s must not fill RAM)."""
        head, tail, total = bytearray(), bytearray(), 0
        while chunk := await stream.read(65536):
            total += len(chunk)
            if len(head) < cap // 2:
                head += chunk[: cap // 2 - len(head)]
            tail = (tail + chunk)[-cap // 2:]
        return bytes(head) if total <= cap // 2 else bytes(head) + b"\n...[output truncated]...\n" + bytes(tail)

    async def interact():
        proc.stdin.write(script.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        out_, err_ = await asyncio.gather(read_capped(proc.stdout), read_capped(proc.stderr))
        await proc.wait()
        return out_, err_

    try:
        out, err = await asyncio.wait_for(interact(), timeout=cfg.timeout_s + 20)
    except TimeoutError:
        proc.kill()                                     # backstop: kill wsl.exe; --die-with-parent ends bwrap
        await proc.wait()
        out, err = b"", b"(sandbox killed by the outer timeout)"
    code = proc.returncode if proc.returncode is not None else -1
    timed_out = code in (124, 137, -9)                  # `timeout -s KILL` exits 137 (128 + SIGKILL)
    return SandboxResult(
        exit_code=code,
        stdout=trim(out.decode("utf-8", errors="replace"), cfg.max_output_chars),
        stderr=trim(err.decode("utf-8", errors="replace"), cfg.max_output_chars // 2),
        timed_out=timed_out,
        duration_s=round(time.monotonic() - start, 2),
    )
