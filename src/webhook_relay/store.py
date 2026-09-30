"""SQLite: incoming events and their deliveries (the "outbox" pattern).

Receiving a webhook only WRITES: the event and one delivery row per matching route, in one
transaction. Sending happens later, in the dispatcher. So the sender gets "202 Accepted" in
milliseconds, and no event is lost if a target is down or the relay restarts.

Delivery lifecycle:
    pending ──claim──► delivering ──2xx──► delivered
       ▲                   │ 5xx/timeout (attempts left) → pending, next_attempt_at = now + backoff
       │                   │ 4xx, or no attempts left     → dead   (kept for manual replay)
       └──── lease expired (process crashed mid-send) ◄─┘
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    event_id     TEXT NOT NULL,         -- the sender's id: the same id twice = the same event (dedup)
    event_type   TEXT NOT NULL,
    received_at  REAL NOT NULL,
    body         TEXT NOT NULL,         -- raw JSON as received, delivered unchanged
    UNIQUE (source, event_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    id               INTEGER PRIMARY KEY,
    event_pk         INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    route            TEXT NOT NULL,
    target           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',   -- pending | delivering | delivered | dead
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL,
    lease_until      REAL,
    last_status      INTEGER,
    last_error       TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    delivered_at     REAL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_events_received ON events (received_at);
"""

STATUSES = ("pending", "delivering", "delivered", "dead")


@dataclass(frozen=True)
class Delivery:
    id: int
    event_pk: int
    route: str
    target: str
    status: str
    attempts: int
    next_attempt_at: float
    last_status: int | None
    last_error: str
    delivered_at: float | None
    # from the event
    source: str = ""
    event_id: str = ""
    event_type: str = ""
    body: str = ""


class Store:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()  # one connection for everything: no statement may slip into another's transaction

    @classmethod
    async def open(cls, path: Path | str) -> Store:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    @contextlib.asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            await self._conn.execute("COMMIT")

    async def add_event(
        self, source: str, event_id: str, event_type: str, body: str, routes: Iterable[tuple[str, str]], now: float
    ) -> tuple[int, bool, int]:
        """Stores an event and its deliveries. Returns (event pk, duplicate?, deliveries created)."""
        async with self._tx() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO events (source, event_id, event_type, received_at, body) VALUES (?, ?, ?, ?, ?)",
                (source, event_id, event_type, now, body),
            )
            if cursor.rowcount == 0:  # UNIQUE(source, event_id) hit: the sender retried an event we already have
                async with db.execute(
                    "SELECT id FROM events WHERE source = ? AND event_id = ?", (source, event_id)
                ) as c:
                    return (await c.fetchone())["id"], True, 0
            pk = cursor.lastrowid
            rows = [(pk, name, target, now, now) for name, target in routes]
            await db.executemany(
                "INSERT INTO deliveries (event_pk, route, target, next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            return pk, False, len(rows)

    async def claim(self, now: float, limit: int, lease_seconds: float) -> list[Delivery]:
        """Takes due deliveries for sending. A crashed sender's deliveries come back after the lease."""
        async with self._tx() as db:
            await db.execute(
                "UPDATE deliveries SET status = 'pending', lease_until = NULL "
                "WHERE status = 'delivering' AND lease_until < ?",
                (now,),
            )
            async with db.execute(
                "UPDATE deliveries SET status = 'delivering', lease_until = ? "
                "WHERE id IN (SELECT id FROM deliveries WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, id LIMIT ?) RETURNING id",
                (now + lease_seconds, now, limit),
            ) as cursor:
                ids = [row["id"] async for row in cursor]
        return [d for d in [await self.delivery(i) for i in sorted(ids)] if d]

    async def _finish(self, delivery_id: int, sql: str, params: tuple) -> None:
        async with self._tx() as db:
            await db.execute(sql, (*params, delivery_id))

    async def mark_delivered(self, delivery_id: int, status_code: int, now: float) -> None:
        await self._finish(
            delivery_id,
            "UPDATE deliveries SET status = 'delivered', attempts = attempts + 1, last_status = ?, last_error = '', "
            "delivered_at = ?, lease_until = NULL WHERE id = ?",
            (status_code, now),
        )

    async def mark_retry(self, delivery_id: int, status_code: int | None, error: str, next_at: float) -> None:
        await self._finish(
            delivery_id,
            "UPDATE deliveries SET status = 'pending', attempts = attempts + 1, last_status = ?, last_error = ?, "
            "next_attempt_at = ?, lease_until = NULL WHERE id = ?",
            (status_code, error, next_at),
        )

    async def mark_dead(self, delivery_id: int, status_code: int | None, error: str) -> None:
        await self._finish(
            delivery_id,
            "UPDATE deliveries SET status = 'dead', attempts = attempts + 1, last_status = ?, last_error = ?, "
            "lease_until = NULL WHERE id = ?",
            (status_code, error),
        )

    async def replay_delivery(self, delivery_id: int, now: float) -> bool:
        """Sends a finished (dead or delivered) delivery again, from attempt one."""
        async with self._tx() as db:
            cursor = await db.execute(
                "UPDATE deliveries SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = '', "
                "delivered_at = NULL WHERE id = ? AND status IN ('dead', 'delivered')",
                (now, delivery_id),
            )
            return cursor.rowcount == 1

    async def replay_dead(self, now: float) -> int:
        async with self._tx() as db:
            cursor = await db.execute(
                "UPDATE deliveries SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = '' "
                "WHERE status = 'dead'",
                (now,),
            )
            return cursor.rowcount

    async def delivery(self, delivery_id: int) -> Delivery | None:
        async with self._conn.execute(
            "SELECT d.*, e.source, e.event_id, e.event_type, e.body FROM deliveries d "
            "JOIN events e ON e.id = d.event_pk WHERE d.id = ?",
            (delivery_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _delivery(row) if row else None

    async def deliveries(
        self, status: str | None = None, event_pk: int | None = None, limit: int = 50
    ) -> list[Delivery]:
        sql = (
            "SELECT d.*, e.source, e.event_id, e.event_type, '' AS body FROM deliveries d "
            "JOIN events e ON e.id = d.event_pk WHERE 1 = 1"
        )
        params: list = []
        if status:
            sql += " AND d.status = ?"
            params.append(status)
        if event_pk is not None:
            sql += " AND d.event_pk = ?"
            params.append(event_pk)
        sql += " ORDER BY d.id DESC LIMIT ?"
        async with self._conn.execute(sql, (*params, limit)) as cursor:
            return [_delivery(row) async for row in cursor]

    async def events(self, source: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT id, source, event_id, event_type, received_at FROM events"
        params: tuple = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        async with self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)) as cursor:
            return [dict(row) async for row in cursor]

    async def event(self, pk: int) -> dict | None:
        async with self._conn.execute("SELECT * FROM events WHERE id = ?", (pk,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        event = dict(row)
        event["body"] = json.loads(event["body"])
        event["deliveries"] = [d.__dict__ | {"body": None} for d in await self.deliveries(event_pk=pk, limit=100)]
        return event

    async def stats(self, now: float) -> dict:
        counts = dict.fromkeys(STATUSES, 0)
        async with self._conn.execute("SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status") as cursor:
            async for row in cursor:
                counts[row["status"]] = row["n"]
        async with self._conn.execute("SELECT MIN(created_at) AS t FROM deliveries WHERE status = 'pending'") as c:
            oldest = (await c.fetchone())["t"]
        async with self._conn.execute("SELECT COUNT(*) AS n FROM events") as c:
            events = (await c.fetchone())["n"]
        return {"events": events, "deliveries": counts, "oldest_pending_seconds": round(now - oldest) if oldest else 0}

    async def cleanup(self, older_than: float) -> int:
        """Deletes old events whose deliveries all succeeded (dead ones are kept until someone looks at them)."""
        async with self._tx() as db:
            cursor = await db.execute(
                "DELETE FROM events WHERE received_at < ? AND NOT EXISTS "
                "(SELECT 1 FROM deliveries d WHERE d.event_pk = events.id AND d.status != 'delivered')",
                (older_than,),
            )
            return cursor.rowcount


def _delivery(row: aiosqlite.Row) -> Delivery:
    return Delivery(
        id=row["id"],
        event_pk=row["event_pk"],
        route=row["route"],
        target=row["target"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_status=row["last_status"],
        last_error=row["last_error"],
        delivered_at=row["delivered_at"],
        source=row["source"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        body=row["body"],
    )
