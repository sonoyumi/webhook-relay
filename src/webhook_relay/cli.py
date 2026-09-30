"""Command line.

webhook-relay serve                          receive + deliver (HOST:PORT)
webhook-relay check                          validate relay.toml and the secrets in .env
webhook-relay stats                          deliveries by status
webhook-relay replay --delivery 12 | --dead  send again
webhook-relay sign --source stripe body.json print a signed curl command (for testing a source)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import shlex
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from webhook_relay import __version__
from webhook_relay.config import ConfigError, Relay, Settings, load_relay, load_settings
from webhook_relay.signatures import sign_headers
from webhook_relay.store import Store


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webhook-relay", description="Reliable webhook relay")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="receive and deliver webhooks")
    sub.add_parser("check", help="validate relay.toml and secrets")
    sub.add_parser("stats", help="deliveries by status")
    replay = sub.add_parser("replay", help="send deliveries again")
    group = replay.add_mutually_exclusive_group(required=True)
    group.add_argument("--delivery", type=int, help="one delivery id")
    group.add_argument("--dead", action="store_true", help="every dead delivery")
    sign = sub.add_parser("sign", help="print a signed curl command for testing")
    sign.add_argument("--source", required=True)
    sign.add_argument("file", type=Path, help="JSON body")
    sign.add_argument("--url", default="", help="relay address (default http://HOST:PORT)")
    return p


def print_config(relay: Relay) -> None:
    for s in relay.sources.values():
        routes = [r for r in relay.routes if r.source == s.name]
        print(f"POST /hooks/{s.name}  ({s.scheme})")
        for r in routes:
            events = ", ".join(r.events) if r.events else "все события"
            signed = " · подпись" if r.secret else ""
            print(f"   → {r.name}: {r.target}  [{events}]{signed}")
        if not routes:
            print("   → маршрутов нет: события сохраняются, но никуда не доставляются")


async def _stats(settings: Settings) -> int:
    store = await Store.open(settings.database_path)
    try:
        s = await store.stats(time.time())
    finally:
        await store.close()
    d = s["deliveries"]
    print(f"Событий: {s['events']}")
    print(f"Доставлено: {d['delivered']} · ждут: {d['pending']} · в процессе: {d['delivering']} · мёртвые: {d['dead']}")
    if s["oldest_pending_seconds"]:
        print(f"Самая старая ожидающая доставка: {s['oldest_pending_seconds']} с")
    return 1 if d["dead"] else 0


async def _replay(settings: Settings, delivery: int | None) -> int:
    store = await Store.open(settings.database_path)
    try:
        if delivery is not None:
            ok = await store.replay_delivery(delivery, time.time())
            print(f"Доставка {delivery} поставлена в очередь" if ok else f"Доставку {delivery} нельзя повторить")
            return 0 if ok else 1
        print(f"Поставлено в очередь мёртвых доставок: {await store.replay_dead(time.time())}")
        return 0
    finally:
        await store.close()


def signed_curl(relay: Relay, source: str, body: bytes, url: str, now: float) -> str:
    src = relay.sources[source]
    headers = {"Content-Type": "application/json", **sign_headers(src.scheme, src.secret, body, now)}
    if src.scheme == "github":
        headers["X-GitHub-Event"] = "ping"
        headers["X-GitHub-Delivery"] = f"test-{int(now)}"
    parts = ["curl", "-sS", "-X", "POST", f"{url}/hooks/{source}"]
    for key, value in headers.items():
        parts += ["-H", f"{key}: {value}"]
    parts += ["--data-binary", body.decode()]
    return " ".join(shlex.quote(p) for p in parts)


async def serve(settings: Settings, relay: Relay) -> None:
    import httpx
    import uvicorn

    from webhook_relay.api import create_app
    from webhook_relay.dispatcher import Dispatcher

    store = await Store.open(settings.database_path)
    async with httpx.AsyncClient() as client:
        stop = asyncio.Event()
        dispatcher = asyncio.create_task(Dispatcher(store, relay, settings, client).run(stop))
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings, relay, store), host=settings.host, port=settings.port, log_level="warning"
            )
        )
        logging.getLogger(__name__).info(
            "Relay on http://%s:%s, %d sources, %d routes",
            settings.host,
            settings.port,
            len(relay.sources),
            len(relay.routes),
        )
        try:
            await server.serve()
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher
            await store.close()


def run(argv: Sequence[str] | None = None, settings: Settings | None = None, env: dict | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = settings or load_settings()
        relay = load_relay(settings.config_file, env)
    except ConfigError as exc:
        print("Ошибки в настройках:", file=sys.stderr)
        for error in exc.errors:
            print(f"  {error}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Ошибка настроек: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "check":
        print("Настройки в порядке.")
        print_config(relay)
        if not settings.admin_key:
            print("Внимание: ADMIN_KEY пуст — админ-API выключен.")
        return 0
    if args.command == "stats":
        return asyncio.run(_stats(settings))
    if args.command == "replay":
        return asyncio.run(_replay(settings, args.delivery))
    if args.command == "sign":
        if args.source not in relay.sources:
            print(f"Нет источника «{args.source}» в {settings.config_file}", file=sys.stderr)
            return 2
        body = args.file.read_bytes()
        try:
            json.loads(body)
        except json.JSONDecodeError:
            print(f"{args.file} — не JSON", file=sys.stderr)
            return 2
        print(signed_curl(relay, args.source, body, args.url or f"http://{settings.host}:{settings.port}", time.time()))
        return 0
    asyncio.run(serve(settings, relay))
    return 0


def main() -> int:
    return run()
