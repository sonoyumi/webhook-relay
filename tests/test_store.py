import json

from conftest import NOW


async def add(store, event_id="evt_1", routes=(("crm", "https://crm.it"), ("ci", "https://ci.it")), now=NOW):
    return await store.add_event("stripe", event_id, "invoice.paid", json.dumps({"id": event_id}), routes, now)


async def test_event_and_deliveries_in_one_go_and_dedup(store):
    pk, duplicate, created = await add(store)
    assert (duplicate, created) == (False, 2)
    again = await add(store)
    assert again == (pk, True, 0)
    assert (await store.stats(NOW))["deliveries"]["pending"] == 2


async def test_claim_takes_each_delivery_once(store):
    await add(store)
    first = await store.claim(NOW, limit=10, lease_seconds=60)
    assert [d.route for d in first] == ["crm", "ci"] and first[0].body == '{"id": "evt_1"}'
    assert await store.claim(NOW, limit=10, lease_seconds=60) == []  # already "delivering"


async def test_lease_returns_deliveries_of_a_crashed_sender(store):
    await add(store, routes=(("crm", "https://crm.it"),))
    [d] = await store.claim(NOW, 10, lease_seconds=60)
    assert await store.claim(NOW + 30, 10, 60) == []
    [again] = await store.claim(NOW + 61, 10, 60)  # the first sender never finished
    assert again.id == d.id and again.status == "delivering"


async def test_not_due_yet(store):
    await add(store, routes=(("crm", "https://crm.it"),))
    [d] = await store.claim(NOW, 10, 60)
    await store.mark_retry(d.id, 503, "HTTP 503", next_at=NOW + 120)
    assert await store.claim(NOW + 60, 10, 60) == []
    [retry] = await store.claim(NOW + 120, 10, 60)
    assert retry.attempts == 1 and retry.last_error == "HTTP 503"


async def test_replay_only_finished_and_cleanup_keeps_dead(store):
    pk_ok, _, _ = await add(store, "evt_ok", routes=(("crm", "https://crm.it"),))
    await add(store, "evt_dead", routes=(("crm", "https://crm.it"),))
    ok, dead = await store.claim(NOW, 10, 60)
    await store.mark_delivered(ok.id, 200, NOW)
    await store.mark_dead(dead.id, 400, "HTTP 400")
    assert (await store.stats(NOW))["deliveries"] == {"pending": 0, "delivering": 0, "delivered": 1, "dead": 1}

    assert await store.cleanup(NOW + 1) == 1  # only the fully delivered event
    assert await store.event(pk_ok) is None
    assert await store.replay_dead(NOW) == 1
    [replayed] = await store.claim(NOW, 10, 60)
    assert replayed.id == dead.id and replayed.attempts == 0
    assert not await store.replay_delivery(replayed.id, NOW)  # in flight: cannot replay


async def test_event_details(store):
    pk, _, _ = await add(store)
    event = await store.event(pk)
    assert event["body"] == {"id": "evt_1"} and [d["route"] for d in event["deliveries"]] == ["ci", "crm"]
    assert (await store.events("stripe"))[0]["event_id"] == "evt_1"
