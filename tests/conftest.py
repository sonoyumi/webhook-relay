"""Shared fixtures: a relay config with secrets, a store in memory, a controllable clock and a fake internet."""

from pathlib import Path

import httpx
import pytest

from webhook_relay.config import Settings, load_relay
from webhook_relay.store import Store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
ENV = {
    "STRIPE_WEBHOOK_SECRET": "whsec_test",
    "GITHUB_WEBHOOK_SECRET": "gh_test",
    "FORM_WEBHOOK_SECRET": "form_test",
    "CRM_RELAY_SECRET": "crm_test",
    "LEADS_RELAY_SECRET": "leads_test",
}
NOW = 1_790_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTargets:
    """The receivers' side: url -> Response, exception or callable; records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url), httpx.Response(200))
        if isinstance(route, Exception):
            raise route
        return route(request) if callable(route) else route


@pytest.fixture
def relay():
    return load_relay(ROOT / "relay.example.toml", ENV)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        config_file=ROOT / "relay.example.toml",
        database_path=tmp_path / "relay.db",
        admin_key="admin_test",
        max_attempts=4,
        retry_base_seconds=30,
        retry_max_seconds=600,
        concurrency=2,
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


@pytest.fixture
def targets():
    return FakeTargets()


@pytest.fixture
async def http(targets):
    async with httpx.AsyncClient(transport=httpx.MockTransport(targets.handler)) as client:
        yield client
