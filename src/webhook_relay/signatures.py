"""Checking that a webhook really comes from the sender, and signing what we send on.

Every sender signs the raw body with a shared secret (HMAC-SHA256). If a single byte changes,
the signature no longer matches. Schemes that include a timestamp also stop "replay attacks":
an old, captured request re-sent later is refused because its timestamp is too old.

    stripe  Stripe-Signature: t=<unix>,v1=<hex>    signed text: "<t>.<body>"
    github  X-Hub-Signature-256: sha256=<hex>        signed text: <body>
    hmac    X-Timestamp: <unix>, X-Signature: sha256=<hex>, signed text: "<t>.<body>"  (our own / generic)
    token   X-Webhook-Token: <secret>                simple shared token (no signature, use only over HTTPS)
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

SCHEMES = ("stripe", "github", "hmac", "token")


class SignatureError(ValueError):
    pass


def hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _fresh(timestamp: str, now: float, tolerance: int) -> int:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("missing or bad timestamp") from None
    if abs(now - ts) > tolerance:
        raise SignatureError("timestamp outside the tolerance window (replayed or clock skew)")
    return ts


def verify(scheme: str, secret: str, body: bytes, headers: Mapping[str, str], now: float, tolerance: int = 300) -> None:
    """Raises SignatureError if the request is not authentic. Header names are case-insensitive."""
    h = {k.lower(): v for k, v in headers.items()}
    if scheme == "stripe":
        parts: dict[str, list[str]] = {}
        for item in h.get("stripe-signature", "").split(","):
            key, _, value = item.strip().partition("=")
            parts.setdefault(key, []).append(value)
        ts = _fresh((parts.get("t") or [""])[0], now, tolerance)
        expected = hmac_hex(secret, f"{ts}.".encode() + body)
        # Stripe may send several v1 signatures while a secret is being rotated: any match is enough.
        if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
            raise SignatureError("bad signature")
    elif scheme == "github":
        received = h.get("x-hub-signature-256", "")
        if not hmac.compare_digest("sha256=" + hmac_hex(secret, body), received):
            raise SignatureError("bad signature")
    elif scheme == "hmac":
        ts = _fresh(h.get("x-timestamp", ""), now, tolerance)
        expected = "sha256=" + hmac_hex(secret, f"{ts}.".encode() + body)
        if not hmac.compare_digest(expected, h.get("x-signature", "")):
            raise SignatureError("bad signature")
    elif scheme == "token":
        if not hmac.compare_digest(secret, h.get("x-webhook-token", "")):
            raise SignatureError("bad token")
    else:
        raise SignatureError(f"unknown scheme {scheme!r}")


def sign_headers(scheme: str, secret: str, body: bytes, now: float) -> dict[str, str]:
    """Headers a sender would add. Used for our own outgoing deliveries (hmac) and by `webhook-relay sign`."""
    ts = int(now)
    if scheme == "stripe":
        return {"Stripe-Signature": f"t={ts},v1={hmac_hex(secret, f'{ts}.'.encode() + body)}"}
    if scheme == "github":
        return {"X-Hub-Signature-256": "sha256=" + hmac_hex(secret, body)}
    if scheme == "hmac":
        return {"X-Timestamp": str(ts), "X-Signature": "sha256=" + hmac_hex(secret, f"{ts}.".encode() + body)}
    if scheme == "token":
        return {"X-Webhook-Token": secret}
    raise SignatureError(f"unknown scheme {scheme!r}")
