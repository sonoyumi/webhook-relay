"""relay.toml: where webhooks come from (sources) and where they go (routes). Secrets live in .env.

[[source]]
name = "stripe"                         # URL: POST /hooks/stripe
scheme = "stripe"                       # stripe | github | hmac | token
secret_env = "STRIPE_WEBHOOK_SECRET"    # the secret itself is in .env, never in this file
event_id = "id"                         # JSON field with a unique event id (dots for nesting: "data.id")
event_type = "type"                     # JSON field with the event type

[[route]]
name = "crm"
source = "stripe"
events = ["checkout.session.*"]         # shell-style patterns; empty or missing = every event
target = "https://crm.example.it/hooks/payments"
secret_env = "CRM_SECRET"               # optional: we sign what we deliver (hmac scheme)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from dotenv import load_dotenv

from webhook_relay.signatures import SCHEMES


class ConfigError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Source:
    name: str
    scheme: str
    secret: str = field(repr=False)
    tolerance: int = 300
    event_id: str = "id"  # JSON path; github uses the X-GitHub-Delivery header instead
    event_type: str = "type"  # JSON path; github uses the X-GitHub-Event header instead


@dataclass(frozen=True)
class Route:
    name: str
    source: str
    target: str
    events: tuple[str, ...] = ()
    secret: str = field(default="", repr=False)

    def matches(self, event_type: str) -> bool:
        return not self.events or any(fnmatchcase(event_type, pattern) for pattern in self.events)


@dataclass(frozen=True)
class Relay:
    sources: dict[str, Source]
    routes: tuple[Route, ...]

    def routes_for(self, source: str, event_type: str) -> list[Route]:
        return [r for r in self.routes if r.source == source and r.matches(event_type)]


@dataclass(frozen=True)
class Settings:
    config_file: Path
    database_path: Path
    admin_key: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 8095
    max_body_bytes: int = 1_000_000
    max_attempts: int = 8
    retry_base_seconds: float = 30.0  # 30 s, 1 min, 2 min, 4 min ... capped at retry_max_seconds
    retry_max_seconds: float = 3600.0
    delivery_timeout: float = 10.0
    concurrency: int = 4
    retention_days: int = 30


def _secret(raw: dict, where: str, errors: list[str], env: dict[str, str], required: bool) -> str:
    name = raw.get("secret_env", "")
    if not name:
        if required:
            errors.append(f"{where}: secret_env is required")
        return ""
    value = env.get(name, "").strip()
    if not value:
        errors.append(f"{where}: environment variable {name} is empty (put the secret into .env)")
    return value


def load_relay(path: Path, env: dict[str, str] | None = None) -> Relay:
    env = dict(os.environ) if env is None else env
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"file not found: {path}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"{path.name}: TOML syntax error: {exc}"]) from None
    errors: list[str] = []
    sources: dict[str, Source] = {}
    for raw in doc.get("source", []):
        name = str(raw.get("name", "")).strip()
        where = f"source «{name or '?'}»"
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            errors.append(f"{where}: name is required (letters, digits, - and _: it becomes the URL /hooks/<name>)")
            continue
        if name in sources:
            errors.append(f"{where}: duplicate name")
            continue
        scheme = raw.get("scheme", "")
        if scheme not in SCHEMES:
            errors.append(f"{where}: scheme must be one of {', '.join(SCHEMES)}")
        tolerance = raw.get("tolerance", 300)
        if not isinstance(tolerance, int) or tolerance < 1:
            errors.append(f"{where}: tolerance must be a whole number of seconds")
        secret = _secret(raw, where, errors, env, required=True)
        sources[name] = Source(
            name=name,
            scheme=scheme,
            secret=secret,
            tolerance=tolerance if isinstance(tolerance, int) else 300,
            event_id=str(raw.get("event_id", "id")),
            event_type=str(raw.get("event_type", "type")),
        )
    routes: list[Route] = []
    for raw in doc.get("route", []):
        name = str(raw.get("name", "")).strip()
        where = f"route «{name or '?'}»"
        if not name:
            errors.append(f"{where}: name is required")
        if raw.get("source") not in sources:
            errors.append(f"{where}: source «{raw.get('source', '')}» is not defined")
        target = str(raw.get("target", ""))
        if not target.startswith(("https://", "http://")):
            errors.append(f"{where}: target must be an http(s) URL")
        events = raw.get("events", [])
        if not isinstance(events, list) or not all(isinstance(e, str) and e for e in events):
            errors.append(f"{where}: events must be a list of patterns")
            events = []
        secret = _secret(raw, where, errors, env, required=False)
        routes.append(Route(name, str(raw.get("source", "")), target, tuple(events), secret))
    names = [r.name for r in routes]
    errors.extend(f"route «{n}»: duplicate name" for n in sorted({n for n in names if n and names.count(n) > 1}))
    if not sources:
        errors.append("no [[source]] sections")
    if errors:
        raise ConfigError(errors)
    return Relay(sources, tuple(routes))


def _num(name: str, default: float, cast=int):
    raw = os.getenv(name, "").strip()
    try:
        return cast(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None:
        load_dotenv(env_file)
    return Settings(
        config_file=Path(os.getenv("RELAY_CONFIG", "").strip() or "relay.toml"),
        database_path=Path(os.getenv("DATABASE_PATH", "").strip() or "data/relay.db"),
        admin_key=os.getenv("ADMIN_KEY", "").strip(),
        host=os.getenv("HOST", "").strip() or "127.0.0.1",
        port=_num("PORT", 8095),
        max_body_bytes=_num("MAX_BODY_BYTES", 1_000_000),
        max_attempts=_num("MAX_ATTEMPTS", 8),
        retry_base_seconds=_num("RETRY_BASE_SECONDS", 30.0, float),
        retry_max_seconds=_num("RETRY_MAX_SECONDS", 3600.0, float),
        delivery_timeout=_num("DELIVERY_TIMEOUT", 10.0, float),
        concurrency=_num("CONCURRENCY", 4),
        retention_days=_num("RETENTION_DAYS", 30),
    )
