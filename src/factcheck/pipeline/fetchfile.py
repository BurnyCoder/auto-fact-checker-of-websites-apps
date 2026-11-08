"""`fetch_file`: download raw public data (CSV/JSON/XLSX/PDF/HTML) into a claim's /work folder.

Local context: an HTTP GET that refuses anything that is not the public internet, enforces a size and
time limit, and saves the bytes under a sanitised filename.
Global context: the sandbox has no network, so this audited tool (run by our Python process, not by
the agent) is the only way data reaches it. WebFetch only returns a model-written summary, so this is
also how the agent can read raw tables or exact quotes.

SSRF defence (https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html):
every TCP connection — including each redirect hop — goes through `PublicOnlyBackend.connect_tcp`,
which resolves the host itself, rejects non-global addresses (loopback, RFC 1918, link-local such as the
169.254.169.254 metadata address, CGNAT, the WSL host's 172.x address, IPv6 ULA, ...) and then connects
to the vetted IP. Because the check happens at connect time on the address actually used, a DNS
rebinding trick cannot swap in a private address after validation. TLS still verifies the certificate
against the original hostname because httpcore passes the URL host to start_tls separately.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import ssl
from dataclasses import dataclass
from pathlib import Path

import certifi
import httpcore
import httpx

from factcheck.pipeline.policy import OPEN_POLICY, UrlPolicy

ALLOWED_PORTS = {80, 443}                       # plain web only; no SSH, databases, admin panels
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/csv", "application/javascript")


class BlockedDestination(Exception):
    """Raised when a URL or resolved address is not allowed."""


def is_public_ip(ip_text: str) -> bool:
    """True only for globally routable unicast addresses (IPv4-mapped IPv6 is unwrapped first)."""
    ip = ipaddress.ip_address(ip_text)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:  # ::ffff:127.0.0.1 -> 127.0.0.1
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast  # is_global excludes private, loopback, link-local, CGNAT, ...


async def resolve_public(host: str, port: int) -> str:
    """Resolve `host` and return one address, refusing if ANY resolved address is non-public."""
    if port not in ALLOWED_PORTS:
        raise BlockedDestination(f"port {port} not allowed")
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [info[4][0] for info in infos]        # sockaddr[0] is the IP string for v4 and v6
    if not addresses:
        raise BlockedDestination(f"{host} did not resolve")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:                                            # one private answer is enough to refuse (rebinding)
        raise BlockedDestination(f"{host} resolves to non-public address {bad[0]}")
    return addresses[0]


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that vets every TCP destination before connecting."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()          # httpcore's standard asyncio/anyio backend

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ip = await resolve_public(host, port)          # raises BlockedDestination for private targets
        return await self._inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedDestination("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_only_client(timeout_s: float) -> httpx.AsyncClient:
    """An httpx client whose connections all go through PublicOnlyBackend.

    httpx 0.28 has no public parameter for the network backend, so we replace the transport's
    connection pool (httpx/_transports/default.py builds `self._pool = httpcore.AsyncConnectionPool(...)`;
    httpcore documents `network_backend`: https://www.encode.io/httpcore/network-backends/).
    `trust_env=False` stops system proxy settings from routing around the check.
    """
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl.create_default_context(cafile=certifi.where()),
        network_backend=PublicOnlyBackend(),
        http1=True,
        http2=False,
        retries=0,
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout_s, follow_redirects=True,
                             max_redirects=5, trust_env=False,
                             headers={"User-Agent": "fact-check-websites-apps/0.1 (research fact-checker)"})


def safe_filename(name: str, url: str) -> str:
    """A plain basename for the saved file: no folders, no hidden files, portable characters only."""
    base = Path(name or Path(httpx.URL(url).path).name or "download").name  # strip any directories
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")              # no '..', no hidden files
    return base[:100] or "download"


def _save_exclusive(target: Path, data: bytes) -> Path:
    """Write to a fresh name: never overwrite or follow a file, folder or link the agent created in /work."""
    while target.exists() or target.is_symlink():
        target = target.with_name(f"{target.stem}_{os.urandom(3).hex()}{target.suffix}")
    with open(target, "xb") as fh:                          # "x": fail if it appeared in the meantime
        fh.write(data)
    return target


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one download, rendered for the model by `as_tool_text`."""

    ok: bool
    url: str
    final_url: str = ""
    path_in_sandbox: str = ""
    size: int = 0
    content_type: str = ""
    preview: str = ""
    error: str = ""

    def as_tool_text(self) -> str:
        if not self.ok:
            return f"fetch_file FAILED for {self.url}: {self.error}"
        return (f"saved {self.size} bytes to {self.path_in_sandbox} (content-type: {self.content_type}; "
                f"final URL: {self.final_url})\n--- preview ---\n{self.preview}")


async def fetch_file(url: str, filename: str, dest_dir: Path, policy: UrlPolicy = OPEN_POLICY,
                     max_bytes: int = 25 * 1024**2, timeout_s: float = 30.0) -> FetchResult:
    """Download `url` into `dest_dir/filename` (dest_dir is the claim's workspace, seen as /work)."""
    reason = policy.check(url)                         # eval leakage blocklists (empty in the app)
    if reason:
        return FetchResult(ok=False, url=url, error=f"blocked by policy: {reason}")

    async def check_each_request(request: httpx.Request) -> None:   # runs for redirects too
        hop_reason = policy.check(str(request.url))
        if hop_reason:
            raise BlockedDestination(f"redirect blocked by policy: {hop_reason}")

    target = dest_dir / safe_filename(filename, url)
    try:
        async with public_only_client(timeout_s) as client:
            client.event_hooks["request"] = [check_each_request]
            async with asyncio.timeout(timeout_s):     # total time, not just per-read
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise BlockedDestination(f"file too large ({declared} bytes > {max_bytes})")
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:           # servers can lie about or omit content-length
                            raise BlockedDestination(f"file too large (> {max_bytes} bytes)")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
                    final_url = str(resp.url)
    except (BlockedDestination, httpx.HTTPError, OSError, TimeoutError) as exc:
        return FetchResult(ok=False, url=url, error=f"{type(exc).__name__}: {exc}")
    try:
        target = await asyncio.to_thread(_save_exclusive, target, data)   # up to 25 MB: keep the event loop free
    except OSError as exc:
        return FetchResult(ok=False, url=url, error=f"could not save file: {exc}")
    if ctype.startswith(TEXT_TYPES) or target.suffix.lower() in (".csv", ".json", ".txt", ".html", ".xml", ".md"):
        preview = data[:1500].decode("utf-8", errors="replace")
    else:
        preview = f"(binary {ctype or 'data'}; first bytes {data[:16]!r}) — parse it with the sandbox tools"
    return FetchResult(ok=True, url=url, final_url=final_url, path_in_sandbox=f"/work/{target.name}",
                       size=len(data), content_type=ctype, preview=preview)
