"""Tests for the URL policy and the SSRF-safe `fetch_file` download tool."""

from __future__ import annotations

import pytest

from factcheck.pipeline.fetchfile import BlockedDestination, fetch_file, is_public_ip, resolve_public, safe_filename
from factcheck.pipeline.policy import OPEN_POLICY, UrlPolicy, host_matches


# ---------- policy ----------
def test_host_matches_subdomains_only():
    assert host_matches("www.politifact.com", "politifact.com")
    assert host_matches("politifact.com", "politifact.com")
    assert not host_matches("notpolitifact.com", "politifact.com")


def test_open_policy_allows_arxiv_and_blocks_non_http():
    assert OPEN_POLICY.check("https://arxiv.org/abs/2403.18802") is None
    assert "scheme" in OPEN_POLICY.check("file:///C:/Windows/win.ini")


def test_eval_policy_blocks_domains_terms_and_archives():
    p = UrlPolicy(blocked_domains=frozenset({"politifact.com", "huggingface.co"}), blocked_path_terms=("averitec",))
    assert p.check("https://www.politifact.com/factchecks/2021/x/") is not None
    assert p.check("https://huggingface.co/datasets/foo") is not None
    assert p.check("https://example.org/data/AVeriTeC_dev.json") is not None        # answer-key guard
    assert p.check("https://example.org/%61veritec") is not None                    # percent-encoded term
    assert p.check("https://web.archive.org/web/2021/https://www.politifact.com/x") is not None
    assert p.check("https://www.reuters.com/world/") is None


# ---------- address checks ----------
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.20.0.1", "169.254.169.254",
                                "100.64.0.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1"])
def test_non_public_addresses_rejected(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_accepted(ip):
    assert is_public_ip(ip)


async def test_resolve_refuses_localhost_and_odd_ports():
    with pytest.raises(BlockedDestination):
        await resolve_public("localhost", 443)
    with pytest.raises(BlockedDestination):
        await resolve_public("8.8.8.8", 22)


def test_safe_filename_strips_paths():
    assert safe_filename("../../evil.sh", "https://x.org/a") == "evil.sh"
    assert safe_filename("", "https://x.org/data/table.csv") == "table.csv"
    assert safe_filename(".bashrc", "https://x.org/") == "bashrc"


# ---------- fetch_file ----------
async def test_fetch_blocks_loopback_server(tmp_path):
    res = await fetch_file("http://127.0.0.1:80/", "x.html", tmp_path)
    assert not res.ok and "non-public" in res.error


async def test_fetch_blocks_policy_before_network(tmp_path):
    p = UrlPolicy(blocked_domains=frozenset({"example.com"}))
    res = await fetch_file("https://example.com/", "x.html", tmp_path, policy=p)
    assert not res.ok and "policy" in res.error


@pytest.mark.network
async def test_fetch_public_page(tmp_path):
    res = await fetch_file("https://example.com/", "example.html", tmp_path)
    assert res.ok, res.error
    assert res.path_in_sandbox == "/work/example.html" and (tmp_path / "example.html").exists()
    assert "Example Domain" in res.preview


@pytest.fixture
def local_server():
    """A tiny HTTP server on 127.0.0.1: /ok returns data, /redir 302-redirects to an 'internal' host."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            if self.path == "/ok":
                body = b"year,value\n2020,42\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", f"http://internal.test:{self.server.server_port}/ok")
                self.end_headers()

        def log_message(self, *args):  # keep test output quiet
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


@pytest.fixture
def fake_dns(monkeypatch, local_server):
    """Pretend 'public.test' is a public host (resolving to our local server) and anything else is private."""
    import factcheck.pipeline.fetchfile as ff

    async def fake_resolve(host, port):
        if host == "public.test":
            return "127.0.0.1"
        raise BlockedDestination(f"{host} resolves to non-public address 10.0.0.1")

    monkeypatch.setattr(ff, "resolve_public", fake_resolve)
    return local_server


async def test_fetch_through_vetting_backend_saves_file(tmp_path, fake_dns):
    res = await fetch_file(f"http://public.test:{fake_dns}/ok", "table.csv", tmp_path)
    assert res.ok, res.error
    assert (tmp_path / "table.csv").read_text() == "year,value\n2020,42\n"


async def test_fetch_redirect_to_private_is_blocked(tmp_path, fake_dns):
    """A 'public' URL that redirects to a private host is refused at the second hop's connect."""
    res = await fetch_file(f"http://public.test:{fake_dns}/redir", "x.csv", tmp_path)
    assert not res.ok and "non-public" in res.error, res
    assert not (tmp_path / "x.csv").exists()


@pytest.mark.network
async def test_fetch_size_limit(tmp_path):
    res = await fetch_file("https://example.com/", "x.html", tmp_path, max_bytes=100)
    assert not res.ok and "too large" in res.error
