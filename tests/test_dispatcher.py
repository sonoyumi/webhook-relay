import hashlib
import hmac
import json

import httpx
import pytest
from conftest import NOW

from webhook_relay.dispatcher import Dispatcher

CRM = "https://crm.example.it/hooks/payments"


@pytest.fixture
def dispatcher(store, relay, settings, http, clock):
    return Dispatcher(store, relay, settings, http, clock=clock, rng=lambda: 0.5)  # rng 0.5 = no jitter


async def queue(store, route="crm-payments", target=CRM, event_id="evt_1"):
    await store.add_event("stripe", event_id, "invoice.paid", json.dumps({"id": event_id}), [(route, target)], NOW)


async def only(store):
    [d] = await store.deliveries()
    return d


async def test_delivered_with_our_signature(dispatcher, store, targets):
    await queue(store)
    assert await dispatcher.tick() == 1
    d = await only(store)
    assert d.status == "delivered" and d.attempts == 1 and d.last_status == 200
    [request] = targets.requests
    ts, body = request.headers["X-Timestamp"], request.content
    expected = "sha256=" + hmac.new(b"crm_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert request.headers["X-Signature"] == expected  # the receiver can verify us
    assert request.headers["X-Relay-Event-Id"] == "evt_1" and request.headers["X-Relay-Event-Type"] == "invoice.paid"
    assert body == b'{"id": "evt_1"}'  # delivered unchanged


async def test_server_error_is_retried_with_exponential_backoff(dispatcher, store, targets, clock):
    targets.routes[CRM] = httpx.Response(503)
    await queue(store)
    delays = []
    for _ in range(3):
        await dispatcher.tick()
        d = await only(store)
        delays.append(d.next_attempt_at - clock.now)
        clock.now = d.next_attempt_at
    assert delays == [30, 60, 120] and d.status == "pending" and d.last_error == "HTTP 503"
    await dispatcher.tick()  # 4th attempt = max_attempts
    d = await only(store)
    assert d.status == "dead" and d.attempts == 4 and "gave up after 4 attempts" in d.last_error


async def test_retry_after_and_cap(dispatcher, store, targets, clock):
    targets.routes[CRM] = httpx.Response(429, headers={"Retry-After": "7"})
    await queue(store)
    await dispatcher.tick()
    assert (await only(store)).next_attempt_at == clock.now + 7
    assert dispatcher.backoff(20) == 600  # capped by retry_max_seconds


def test_jitter_range(store, relay, settings, http):
    low = Dispatcher(store, relay, settings, http, rng=lambda: 0.0).backoff(1)
    high = Dispatcher(store, relay, settings, http, rng=lambda: 0.999999).backoff(1)
    assert low == 24 and 35.9 < high <= 36  # 30 s ± 20 %
    assert Dispatcher(store, relay, settings, http, rng=lambda: 0.999999).backoff(30) == 600  # never above the cap


async def test_client_errors_are_not_retried(dispatcher, store, targets):
    targets.routes[CRM] = httpx.Response(400)
    await queue(store)
    await dispatcher.tick()
    d = await only(store)
    assert d.status == "dead" and d.attempts == 1 and "not retried" in d.last_error


@pytest.mark.parametrize(
    ("error", "text"),
    [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("down"), "network error: ConnectError")],
)
async def test_network_problems_are_retried(dispatcher, store, targets, error, text):
    targets.routes[CRM] = error
    await queue(store)
    await dispatcher.tick()
    d = await only(store)
    assert d.status == "pending" and d.last_error == text


async def test_route_removed_from_config(dispatcher, store):
    await queue(store, route="old-route")
    await dispatcher.tick()
    assert (await only(store)).last_error == "route no longer in relay.toml"


async def test_unsigned_route_and_parallel_ticks(dispatcher, store, targets):
    for i in range(5):
        await queue(store, route="telegram-notify", target="https://notify.example.it/refunds", event_id=f"e{i}")
    assert await dispatcher.tick() == 5
    assert len(targets.requests) == 5 and "X-Signature" not in targets.requests[0].headers
    assert (await store.stats(NOW))["deliveries"]["delivered"] == 5
