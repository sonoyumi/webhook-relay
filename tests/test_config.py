import pytest
from conftest import ENV, ROOT

from webhook_relay.config import ConfigError, load_relay, load_settings


def test_example_config(relay):
    assert list(relay.sources) == ["stripe", "github", "site-form"]
    assert [r.name for r in relay.routes_for("stripe", "invoice.paid")] == ["crm-payments"]
    assert [r.name for r in relay.routes_for("stripe", "checkout.session.completed")] == ["crm-payments"]
    assert [r.name for r in relay.routes_for("stripe", "charge.refunded")] == ["telegram-notify"]
    assert relay.routes_for("stripe", "customer.created") == []
    assert [r.name for r in relay.routes_for("site-form", "anything")] == ["leads"]  # no patterns = every event
    assert "whsec_test" not in repr(relay)  # secrets never printed


def test_missing_secret_in_env():
    env = {**ENV, "STRIPE_WEBHOOK_SECRET": ""}
    with pytest.raises(ConfigError, match="STRIPE_WEBHOOK_SECRET is empty"):
        load_relay(ROOT / "relay.example.toml", env)


def test_all_errors_at_once(tmp_path):
    path = tmp_path / "relay.toml"
    path.write_text(
        """
[[source]]
name = "a"
scheme = "md5"
secret_env = "A"
[[source]]
name = "bad name!"
scheme = "hmac"
[[route]]
name = "r"
source = "nope"
target = "ftp://x"
events = "invoice.*"
[[route]]
name = "r"
source = "a"
target = "https://x.it"
"""
    )
    with pytest.raises(ConfigError) as err:
        load_relay(path, {"A": "secret"})
    text = "\n".join(err.value.errors)
    assert "source «a»: scheme must be one of" in text
    assert "source «bad name!»: name is required" in text
    assert "route «r»: source «nope» is not defined" in text and "target must be an http(s) URL" in text
    assert "events must be a list" in text and "route «r»: duplicate name" in text


def test_file_problems(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_relay(tmp_path / "none.toml", {})
    (tmp_path / "x.toml").write_text("[[source]\n")
    with pytest.raises(ConfigError, match="TOML syntax"):
        load_relay(tmp_path / "x.toml", {})
    (tmp_path / "y.toml").write_text("")
    with pytest.raises(ConfigError, match="no \\[\\[source\\]\\]"):
        load_relay(tmp_path / "y.toml", {})


def test_settings(monkeypatch):
    for name in (
        "RELAY_CONFIG",
        "DATABASE_PATH",
        "ADMIN_KEY",
        "HOST",
        "PORT",
        "MAX_ATTEMPTS",
        "RETRY_BASE_SECONDS",
        "RETRY_MAX_SECONDS",
        "DELIVERY_TIMEOUT",
        "CONCURRENCY",
        "RETENTION_DAYS",
        "MAX_BODY_BYTES",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ADMIN_KEY", "k")
    s = load_settings(env_file=None)
    assert s.port == 8095 and s.max_attempts == 8 and s.retry_base_seconds == 30.0 and "'k'" not in repr(s)
    monkeypatch.setenv("MAX_ATTEMPTS", "many")
    with pytest.raises(ValueError, match="MAX_ATTEMPTS"):
        load_settings(env_file=None)
