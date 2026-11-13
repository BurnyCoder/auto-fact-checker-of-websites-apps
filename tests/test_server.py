"""Server security and relay tests (FastAPI TestClient + FakeRunner, $0)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from factcheck.pipeline.fake import FakeRunner
from factcheck.pipeline.service import FactCheckService
from factcheck.server.api import create_app, extension_id_from_manifest

ORIGIN = "chrome-extension://testid"
WS = "ws://127.0.0.1/ws"                     # TestClient defaults to ws://testserver, which TrustedHost rejects


@pytest.fixture
def client(settings):
    settings.factcheck_fake_llm = True
    service = FactCheckService(settings, FakeRunner(settings))
    app = create_app(settings, service, allowed_origins={ORIGIN})

    async def _start():                                # the worker pool needs the server's event loop
        service.start()

    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.portal.call(_start)
        c.settings = settings
        yield c


def test_extension_id_is_stable_and_valid():
    ext_id = extension_id_from_manifest()
    assert len(ext_id) == 32 and set(ext_id) <= set("abcdefghijklmnop")


def test_health_is_minimal(client):
    assert client.get("/health").json() == {"ok": True, "app": "fact-check-websites-apps"}


def test_rejects_foreign_host_header(client):
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 400


def test_rejects_wrong_origin(client):
    """A page on another site cannot open our socket (cross-site WebSocket hijacking)."""
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(WS, headers={"origin": "https://evil.example"}):
        pass
    assert exc.value.code == 4403


def test_rejects_foreign_host_on_websocket(client):
    with pytest.raises(WebSocketDenialResponse), client.websocket_connect("/ws", headers={"origin": ORIGIN}):
        pass


def test_rejects_bad_token(client):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(WS, headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "wrong"}))
        ws.receive_text()
    assert exc.value.code == 4401


def test_submit_relays_claims_and_verdicts(client):
    with client.websocket_connect(WS, headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": client.settings.server_token}))
        assert json.loads(ws.receive_text())["type"] == "hello"
        ws.send_text(json.dumps({"type": "submit", "source": {"source_id": "tab1", "title": "t", "url": "https://x.org"},
                                 "blocks": [{"id": "b1", "text": "The tower is 330 m tall. Nice view."}]}))
        seen = []
        while not any(m["type"] == "verdict" for m in seen):
            seen.append(json.loads(ws.receive_text()))
        claims = next(m for m in seen if m["type"] == "claims")["claims"]
        verdict = next(m for m in seen if m["type"] == "verdict")["verdict"]
        assert claims[0]["span"] == "The tower is 330 m tall." and verdict["label"] in (
            "Supported", "Refuted", "Not Enough Evidence", "Conflicting Evidence/Cherrypicking")
        assert 0 <= verdict["confidence"] <= 1 and verdict["confidence_reasoning"]



def test_check_of_unknown_claim_explains_instead_of_hanging(client):
    """Regression (2026-11-13): after an app restart, "Try again" on an old card asked for a claim the new app had never
    seen; nothing came back and the card stayed "Checking…" forever."""
    with client.websocket_connect(WS, headers={"origin": ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": client.settings.server_token}))
        assert json.loads(ws.receive_text())["type"] == "hello"
        ws.send_text(json.dumps({"type": "check", "claim_id": "c0ffee", "source_id": "tab7:doc"}))
        msg = json.loads(ws.receive_text())
        assert (msg["type"], msg["source_id"], msg["claim_id"], msg["status"]) == ("status", "tab7:doc", "c0ffee", "error")
        assert "Alt+Shift+F" in msg["message"]

def test_backend_stop_cancels_startup_tasks(settings):
    """Regression (2026-11-13): quitting left the capture loop pending ("Task was destroyed but it is pending!")."""
    import asyncio
    import socket
    import threading

    from factcheck.backend import Backend

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings.factcheck_fake_llm = True
    cancelled = threading.Event()

    async def loop_forever():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    backend = Backend(settings, port=port)
    backend.add_startup_task(loop_forever)
    backend.start()
    backend.stop()
    assert cancelled.is_set()


def test_disconnect_log_names_the_close_code(client, caplog):
    """Short connect/disconnect pairs were seen in the live test; the close code tells who closed and why."""
    import logging

    with caplog.at_level(logging.INFO, logger="factcheck.server.api"):
        with client.websocket_connect(WS, headers={"origin": ORIGIN}) as ws:
            ws.send_text(json.dumps({"type": "auth", "token": client.settings.server_token}))
            assert json.loads(ws.receive_text())["type"] == "hello"
            ws.close(code=1001)
        client.portal.call(asyncio_sleep_briefly)
    assert any("extension disconnected (code 1001" in r.getMessage() for r in caplog.records)


async def asyncio_sleep_briefly():
    import asyncio

    await asyncio.sleep(0.2)


def test_second_copy_detects_the_running_app():
    """Double-clicking the launcher twice: the second copy must notice the first (GET /health names the app) and stop
    at once, instead of failing to bind the port after a 20 s timeout with a traceback."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from factcheck.app import already_running

    def serve(body: dict) -> ThreadingHTTPServer:
        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *_):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    ours, other = serve({"ok": True, "app": "fact-check-websites-apps"}), serve({"ok": True, "app": "something-else"})
    try:
        assert already_running(ours.server_port)
        assert not already_running(other.server_port)            # another program on the port is not "us"
    finally:
        ours.shutdown()
        other.shutdown()
    assert not already_running(ours.server_port)                 # nothing listening
