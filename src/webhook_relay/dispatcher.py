"""Delivering stored events to their targets, with retries.

Retry policy: attempt 1 right away; after a failure wait retry_base × 2^(attempt-1) (30 s, 1 min,
2 min, 4 min …, never more than retry_max), ±20 % random "jitter" so that hundreds of retries
after an outage do not all hit the target in the same second. A `Retry-After` header from the
target wins. 4xx answers (except 408/425/429) mean "your request is wrong": retrying will not
help, so the delivery goes straight to `dead` and waits for a human (replay).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

import httpx

from webhook_relay import __version__
from webhook_relay.config import Relay, Settings
from webhook_relay.signatures import sign_headers
from webhook_relay.store import Delivery, Store

logger = logging.getLogger(__name__)
RETRYABLE = {408, 425, 429}
LEASE_SECONDS = 120
CLEANUP_EVERY = 3600


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None  # the HTTP-date form is rare for webhooks: ignored


class Dispatcher:
    def __init__(
        self,
        store: Store,
        relay: Relay,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.store, self.relay, self.settings, self.client = store, relay, settings, client
        self.clock, self.rng = clock, rng
        self._routes = {r.name: r for r in relay.routes}
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    def backoff(self, attempt: int, hint: float | None = None) -> float:
        s = self.settings
        if hint is not None:
            return min(hint, s.retry_max_seconds)
        delay = s.retry_base_seconds * 2 ** (attempt - 1) * (0.8 + 0.4 * self.rng())  # ±20 % jitter
        return min(delay, s.retry_max_seconds)  # the ceiling comes last: jitter never pushes past it

    def headers_for(self, d: Delivery) -> dict[str, str]:
        body = d.body.encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"webhook-relay/{__version__}",
            "X-Relay-Source": d.source,
            "X-Relay-Event-Id": d.event_id,
            "X-Relay-Event-Type": d.event_type,
            "X-Relay-Delivery": str(d.id),
        }
        route = self._routes.get(d.route)
        if route and route.secret:  # the receiver checks this exactly like we check incoming webhooks ("hmac")
            headers.update(sign_headers("hmac", route.secret, body, self.clock()))
        return headers

    async def deliver(self, d: Delivery) -> str:
        """One attempt. Returns the new status: delivered | pending | dead."""
        attempt = d.attempts + 1
        if d.route not in self._routes:
            await self.store.mark_dead(d.id, None, "route no longer in relay.toml")
            return "dead"
        try:
            response = await self.client.post(
                d.target, content=d.body.encode(), headers=self.headers_for(d), timeout=self.settings.delivery_timeout
            )
        except httpx.TimeoutException:
            status, error, hint = None, "timeout", None
        except httpx.HTTPError as exc:
            status, error, hint = None, f"network error: {type(exc).__name__}", None
        else:
            status, hint = response.status_code, retry_after(response)
            if 200 <= status < 300:
                await self.store.mark_delivered(d.id, status, self.clock())
                return "delivered"
            error = f"HTTP {status}"
            if 400 <= status < 500 and status not in RETRYABLE:
                await self.store.mark_dead(d.id, status, f"{error}: rejected by the target, not retried")
                logger.warning("Delivery %s to %s rejected: %s", d.id, d.route, error)
                return "dead"
        if attempt >= self.settings.max_attempts:
            await self.store.mark_dead(d.id, status, f"{error} (gave up after {attempt} attempts)")
            logger.warning("Delivery %s to %s is dead after %d attempts: %s", d.id, d.route, attempt, error)
            return "dead"
        await self.store.mark_retry(d.id, status, error, self.clock() + self.backoff(attempt, hint))
        return "pending"

    async def _guarded(self, d: Delivery) -> str:
        async with self._semaphore:
            return await self.deliver(d)

    async def tick(self) -> int:
        """Claims what is due and delivers it in parallel. Returns how many deliveries were attempted."""
        due = await self.store.claim(self.clock(), self.settings.concurrency * 4, LEASE_SECONDS)
        if due:
            await asyncio.gather(*(self._guarded(d) for d in due))
        return len(due)

    async def run(self, stop: asyncio.Event, idle_sleep: float = 1.0) -> None:
        last_cleanup = 0.0
        while not stop.is_set():
            try:
                busy = await self.tick()
                if self.clock() - last_cleanup > CLEANUP_EVERY:
                    last_cleanup = self.clock()
                    removed = await self.store.cleanup(self.clock() - self.settings.retention_days * 86400)
                    if removed:
                        logger.info("Removed %d old delivered events", removed)
            except Exception:  # a broken delivery or a database hiccup must not stop the relay
                logger.exception("Dispatcher step failed")
                busy = 0
            if not busy:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_sleep)
                except TimeoutError:
                    pass
