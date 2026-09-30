"""HTTP part: receiving webhooks and the admin API.

POST /hooks/{source}                  a sender delivers an event here
GET  /admin/stats                     counts by status, oldest waiting delivery
GET  /admin/events[?source=]          recent events
GET  /admin/events/{id}               one event with its body and deliveries
GET  /admin/deliveries[?status=dead]  deliveries by status
POST /admin/deliveries/{id}/replay    send one delivery again
POST /admin/replay-dead               send every dead delivery again
GET  /health

No `from __future__ import annotations`: FastAPI reads annotations at runtime.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from webhook_relay import __version__
from webhook_relay.config import Relay, Settings
from webhook_relay.signatures import SignatureError, verify
from webhook_relay.store import STATUSES, Store


def json_path(data: object, path: str) -> str:
    """ "data.object.id" -> data["data"]["object"]["id"] as text, or "" if any step is missing."""
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return ""
        data = data[key]
    return "" if data is None or isinstance(data, dict | list) else str(data)


def create_app(settings: Settings, relay: Relay, store: Store, clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="Webhook relay", version=__version__, docs_url=None, redoc_url=None)

    @app.post("/hooks/{source}")
    async def receive(source: str, request: Request):
        src = relay.sources.get(source)
        if src is None:
            raise HTTPException(404, "unknown source")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(413, "body too large")
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            raise HTTPException(413, "body too large")
        try:
            verify(src.scheme, src.secret, body, request.headers, clock(), src.tolerance)
        except SignatureError as exc:
            raise HTTPException(401, f"invalid signature: {exc}") from None
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a JSON object")

        if src.scheme == "github":
            event_type = request.headers.get("x-github-event", "")
            event_id = request.headers.get("x-github-delivery", "")
        else:
            event_type, event_id = json_path(data, src.event_type), json_path(data, src.event_id)
        # No id from the sender: the body's hash is the id, so an identical re-send is still a duplicate.
        event_id = event_id or "sha256:" + hashlib.sha256(body).hexdigest()[:32]
        event_type = event_type or "unknown"
        routes = [(r.name, r.target) for r in relay.routes_for(source, event_type)]
        pk, duplicate, created = await store.add_event(source, event_id, event_type, body.decode(), routes, clock())
        payload = {"event": pk, "event_id": event_id, "type": event_type, "duplicate": duplicate, "deliveries": created}
        # 200 for a duplicate: the sender must stop retrying it, and nothing new happened.
        return JSONResponse(payload, status_code=status.HTTP_200_OK if duplicate else status.HTTP_202_ACCEPTED)

    def admin(x_admin_key: Annotated[str | None, Header()] = None) -> None:
        if not settings.admin_key:
            raise HTTPException(404, "admin API is disabled: set ADMIN_KEY")
        if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_key):
            raise HTTPException(401, "missing or invalid X-Admin-Key")

    Admin = Annotated[None, Depends(admin)]

    @app.get("/admin/stats")
    async def stats(_: Admin):
        return await store.stats(clock())

    @app.get("/admin/events")
    async def events(_: Admin, source: str | None = None, limit: int = 50):
        return await store.events(source, min(max(limit, 1), 500))

    @app.get("/admin/events/{pk}")
    async def event(pk: int, _: Admin):
        found = await store.event(pk)
        if found is None:
            raise HTTPException(404, "event not found")
        return found

    @app.get("/admin/deliveries")
    async def deliveries(_: Admin, status: str | None = None, limit: int = 50):
        if status is not None and status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        return [d.__dict__ | {"body": None} for d in await store.deliveries(status, limit=min(max(limit, 1), 500))]

    @app.post("/admin/deliveries/{delivery_id}/replay")
    async def replay(delivery_id: int, _: Admin):
        if not await store.replay_delivery(delivery_id, clock()):
            raise HTTPException(409, "only dead or delivered deliveries can be replayed")
        return {"replayed": delivery_id}

    @app.post("/admin/replay-dead")
    async def replay_dead(_: Admin):
        return {"replayed": await store.replay_dead(clock())}

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    return app
