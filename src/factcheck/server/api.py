"""FastAPI app on 127.0.0.1: one authenticated WebSocket per Chrome extension service worker.

Local context: accept a WebSocket only from our extension's origin, require the shared token in the
first message, then relay `submit` / `check` / `cancel` requests to FactCheckService and push back the
service's events for the sources this connection owns.
Global context: the extension's service worker keeps this socket open (MV3 keeps a worker alive while
a WebSocket is active, Chrome 116+: https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).
Security: bound to 127.0.0.1 only; TrustedHostMiddleware rejects other Host headers (DNS rebinding);
Starlette does not check WebSocket Origin, so we do it by hand (cross-site WebSocket hijacking);
the token is compared with hmac.compare_digest; there is no CORS middleware.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.trustedhost import TrustedHostMiddleware

from factcheck.config import PROJECT_ROOT, Settings
from factcheck.models import SourceInfo, TextBlock
from factcheck.pipeline.service import FactCheckService

log = logging.getLogger(__name__)


def extension_id_from_manifest(manifest_path: Path = PROJECT_ROOT / "extension" / "manifest.json") -> str:
    """Chrome extension ID = first 32 hex chars of sha256(public key DER), digits 0-f mapped to a-p
    (https://developer.chrome.com/docs/extensions/reference/manifest/key)."""
    key = json.loads(manifest_path.read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def to_json(event: dict) -> str:
    """Serialise service events (which may hold pydantic models) to JSON text."""
    def default(obj):
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        return str(obj)
    return json.dumps(event, default=default, ensure_ascii=False)


class ConnectionHub:
    """Tracks live extension sockets and routes service events to the socket that owns each source."""

    def __init__(self) -> None:
        self.queues: dict[int, asyncio.Queue] = {}       # connection id -> outgoing event queue
        self.owner: dict[str, int] = {}                  # source_id -> connection id

    @property
    def extension_connected(self) -> bool:
        return bool(self.queues)

    def listener(self, event: dict) -> None:
        """Service listener (runs in the event loop): enqueue the event for the owning connection."""
        conn = self.owner.get(event.get("source_id") or "")
        if conn is not None and conn in self.queues:
            self.queues[conn].put_nowait(event)


def create_app(settings: Settings, service: FactCheckService, allowed_origins: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="fact-check-websites-apps", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    origins = allowed_origins if allowed_origins is not None else {f"chrome-extension://{extension_id_from_manifest()}"}
    hub = ConnectionHub()
    service.listeners.append(hub.listener)
    app.state.hub = hub

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "app": "fact-check-websites-apps"}          # no details without auth

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        origin = ws.headers.get("origin", "")
        if origin not in origins:
            log.warning("rejected WebSocket from origin %r", origin)
            await ws.close(code=4403)
            return
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
        except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            await ws.close(code=4401)
            return
        token = str(first.get("token", "")) if first.get("type") == "auth" else ""
        if not hmac.compare_digest(token.encode(), settings.server_token.encode()):
            log.warning("rejected WebSocket with a bad token")
            await ws.close(code=4401)
            return
        conn = id(ws)
        queue: asyncio.Queue = asyncio.Queue()
        hub.queues[conn] = queue
        await ws.send_text(json.dumps({"type": "hello", "fake": settings.factcheck_fake_llm}))
        log.info("extension connected (%d live)", len(hub.queues))

        async def pump() -> None:                         # service events -> socket
            while True:
                await ws.send_text(to_json(await queue.get()))

        sender = asyncio.create_task(pump())
        close_code = None                                  # 1000 = extension closed it, 1001 = worker/browser went away
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except json.JSONDecodeError:
                    continue                               # ignore malformed frames
                if isinstance(msg, dict):
                    await handle(msg, conn, ws)
        except WebSocketDisconnect as exc:
            close_code = exc.code
        except RuntimeError:
            pass
        finally:
            sender.cancel()
            hub.queues.pop(conn, None)
            for sid in [s for s, c in hub.owner.items() if c == conn]:
                hub.owner.pop(sid, None)
                service.cancel_source(sid)
            log.info("extension disconnected (code %s, %d live)", close_code, len(hub.queues))

    async def handle(msg: dict, conn: int, ws: WebSocket) -> None:
        kind = msg.get("type")
        if kind == "ping":
            await ws.send_text('{"type":"pong"}')
        elif kind == "submit":
            src = msg.get("source") or {}
            source = SourceInfo(kind="chrome", source_id=str(src.get("source_id", ""))[:200],
                                app="chrome", title=str(src.get("title", ""))[:300], url=str(src.get("url", ""))[:2000])
            if not source.source_id:
                return
            hub.owner[source.source_id] = conn
            # no cap on text length or block count (user decision, 2026-11-13): checks are on request, the extension
            # sends only visible or picked text, and the budget ledger bounds what extraction can spend
            blocks = [TextBlock(id=str(b.get("id", ""))[:40], text=str(b.get("text", "")))
                      for b in (msg.get("blocks") or [])]
            asyncio.create_task(service.submit(source, blocks, auto=bool(msg.get("auto", True)),
                                               force=bool(msg.get("force", False))))
        elif kind == "check":
            claim_id = str(msg.get("claim_id", ""))[:64]
            if claim_id in service.claims:
                service.check(claim_id)
            else:                                          # e.g. a card from before the app was restarted
                await ws.send_text(to_json({
                    "type": "status", "source_id": str(msg.get("source_id", ""))[:200], "claim_id": claim_id,
                    "status": "error",
                    "message": "the app was restarted since this page was checked; press Alt+Shift+F to check it again"}))
        elif kind == "cancel":
            sid = str(msg.get("source_id", ""))
            if hub.owner.get(sid) == conn:
                service.cancel_source(sid)

    return app
