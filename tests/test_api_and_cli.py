import json
import shlex

import httpx
import pytest
from conftest import ENV, EXAMPLES, NOW

from webhook_relay import cli
from webhook_relay.api import create_app, json_path
from webhook_relay.dispatcher import Dispatcher
from webhook_relay.signatures import sign_headers

CHECKOUT = (EXAMPLES / "stripe_checkout.json").read_bytes()
ADMIN = {"X-Admin-Key": "admin_test"}


@pytest.fixture
async def api(settings, relay, store, clock):
    app = create_app(settings, relay, store, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as client:
        yield client


def stripe(body=CHECKOUT, secret="whsec_test", now=NOW):
    return {"Content-Type": "application/json", **sign_headers("stripe", secret, body, now)}


def test_json_path():
    data = {"data": {"object": {"id": "cs_1", "n": 5, "list": [1]}}}
    assert json_path(data, "data.object.id") == "cs_1" and json_path(data, "data.object.n") == "5"
    assert json_path(data, "data.missing.id") == "" and json_path(data, "data.object.list") == ""


async def test_receive_route_and_dedup(api):
    r = await api.post("/hooks/stripe", content=CHECKOUT, headers=stripe())
    assert r.status_code == 202
    assert r.json() == {
        "event": 1,
        "event_id": "evt_1QdemoCheckout",
        "type": "checkout.session.completed",
        "duplicate": False,
        "deliveries": 1,
    }
    again = await api.post("/hooks/stripe", content=CHECKOUT, headers=stripe())
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["deliveries"] == 0


@pytest.mark.parametrize(
    ("path", "body", "headers", "code", "detail"),
    [
        ("/hooks/nope", CHECKOUT, {}, 404, "unknown source"),
        ("/hooks/stripe", CHECKOUT, {"Stripe-Signature": "t=1,v1=00"}, 401, "tolerance"),
        ("/hooks/stripe", CHECKOUT, "wrong-secret", 401, "bad signature"),
        ("/hooks/stripe", b"not json", "sign", 400, "must be JSON"),
        ("/hooks/stripe", b"[1, 2]", "sign", 400, "JSON object"),
        ("/hooks/stripe", b"{" + b" " * 2_000_000 + b"}", {}, 413, "too large"),
    ],
)
async def test_rejections(api, path, body, headers, code, detail):
    if headers == "sign":
        headers = stripe(body)
    elif headers == "wrong-secret":
        headers = stripe(body, secret="other")
    r = await api.post(path, content=body, headers=headers)
    assert r.status_code == code and detail in r.json()["detail"]


async def test_github_uses_its_headers(api):
    body = b'{"ref": "refs/heads/main"}'
    headers = {**sign_headers("github", "gh_test", body, NOW), "X-GitHub-Event": "push", "X-GitHub-Delivery": "guid-1"}
    r = await api.post("/hooks/github", content=body, headers=headers)
    assert r.json()["type"] == "push" and r.json()["event_id"] == "guid-1" and r.json()["deliveries"] == 1


async def test_body_hash_is_the_id_when_sender_sends_none(api):
    body = b'{"form": "contatti", "name": "Anna"}'
    headers = sign_headers("hmac", "form_test", body, NOW)
    first = (await api.post("/hooks/site-form", content=body, headers=headers)).json()
    assert first["event_id"].startswith("sha256:")
    assert (await api.post("/hooks/site-form", content=body, headers=headers)).json()["duplicate"] is True


async def test_admin_api(api, settings, relay, store, clock):
    assert (await api.get("/admin/stats")).status_code == 401
    assert (await api.get("/admin/stats", headers={"X-Admin-Key": "guess"})).status_code == 401
    await api.post("/hooks/stripe", content=CHECKOUT, headers=stripe())
    assert (await api.get("/admin/stats", headers=ADMIN)).json()["deliveries"]["pending"] == 1
    event = (await api.get("/admin/events/1", headers=ADMIN)).json()
    assert event["body"]["data"]["object"]["customer_email"] == "anna@example.it"
    assert (await api.post("/admin/deliveries/1/replay", headers=ADMIN)).status_code == 409  # still pending
    assert (await api.get("/admin/deliveries", params={"status": "lost"}, headers=ADMIN)).status_code == 422
    assert (await api.get("/admin/events/99", headers=ADMIN)).status_code == 404


async def test_admin_disabled_without_key(settings, relay, store):
    app = create_app(settings.__class__(**{**settings.__dict__, "admin_key": ""}), relay, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay") as client:
        r = await client.get("/admin/stats", headers={"X-Admin-Key": ""})
    assert r.status_code == 404 and "disabled" in r.json()["detail"]


async def test_end_to_end_receive_then_deliver(api, settings, relay, store, clock, targets, http):
    await api.post("/hooks/stripe", content=CHECKOUT, headers=stripe())
    assert await Dispatcher(store, relay, settings, http, clock=clock).tick() == 1
    [request] = targets.requests
    assert str(request.url) == "https://crm.example.it/hooks/payments" and request.content == CHECKOUT
    assert (await api.get("/admin/deliveries", params={"status": "delivered"}, headers=ADMIN)).json()[0]["id"] == 1


def test_cli_check_and_signed_curl(settings, relay, capsys, monkeypatch):
    assert cli.run(["check"], settings=settings, env=ENV) == 0
    out = capsys.readouterr().out
    assert "POST /hooks/stripe  (stripe)" in out and "crm-payments" in out and "· подпись" in out
    command = cli.signed_curl(relay, "stripe", CHECKOUT, "http://127.0.0.1:8095", NOW)
    parts = shlex.split(command)
    headers = dict(parts[i + 1].split(": ", 1) for i, p in enumerate(parts) if p == "-H")
    from webhook_relay.signatures import verify

    verify("stripe", "whsec_test", parts[-1].encode(), headers, NOW)  # the printed command really passes
    assert parts[4] == "http://127.0.0.1:8095/hooks/stripe" and json.loads(parts[-1])["id"] == "evt_1QdemoCheckout"


def test_cli_config_error(settings, capsys):
    assert cli.run(["check"], settings=settings, env={}) == 2
    assert "STRIPE_WEBHOOK_SECRET is empty" in capsys.readouterr().err


async def test_cli_stats_and_replay(settings, capsys):
    from webhook_relay.store import Store

    store = await Store.open(settings.database_path)
    await store.add_event("stripe", "e1", "x", "{}", [("crm-payments", "https://crm.it")], NOW)
    [d] = await store.claim(NOW, 10, 60)
    await store.mark_dead(d.id, 400, "HTTP 400")
    await store.close()
    assert await cli._stats(settings) == 1
    assert "мёртвые: 1" in capsys.readouterr().out
    assert await cli._replay(settings, d.id) == 0 and await cli._replay(settings, d.id) == 1
