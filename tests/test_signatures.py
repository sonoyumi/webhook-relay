import pytest
from conftest import NOW

from webhook_relay.signatures import SCHEMES, SignatureError, hmac_hex, sign_headers, verify

BODY = b'{"id": "evt_1", "type": "invoice.paid"}'


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_scheme_round_trips(scheme):
    headers = sign_headers(scheme, "s3cret", BODY, NOW)
    verify(scheme, "s3cret", BODY, headers, NOW + 10)
    lower = {k.lower(): v for k, v in headers.items()}
    verify(scheme, "s3cret", BODY, lower, NOW)  # header names are case-insensitive in HTTP


@pytest.mark.parametrize("scheme", SCHEMES)
def test_wrong_secret_is_refused(scheme):
    with pytest.raises(SignatureError):
        verify(scheme, "other", BODY, sign_headers(scheme, "s3cret", BODY, NOW), NOW)


@pytest.mark.parametrize("scheme", ["stripe", "github", "hmac"])
def test_changed_body_is_refused(scheme):
    headers = sign_headers(scheme, "s3cret", BODY, NOW)
    with pytest.raises(SignatureError, match="bad signature"):
        verify(scheme, "s3cret", BODY.replace(b"evt_1", b"evt_2"), headers, NOW)


@pytest.mark.parametrize("scheme", ["stripe", "hmac"])
def test_old_request_is_refused_as_replay(scheme):
    headers = sign_headers(scheme, "s3cret", BODY, NOW)
    with pytest.raises(SignatureError, match="tolerance"):
        verify(scheme, "s3cret", BODY, headers, NOW + 301, tolerance=300)


def test_stripe_accepts_any_of_several_signatures_during_rotation():
    good = hmac_hex("new", f"{int(NOW)}.".encode() + BODY)
    header = {"Stripe-Signature": f"t={int(NOW)},v1=deadbeef,v1={good}"}
    verify("stripe", "new", BODY, header, NOW)


def test_matches_stripe_documented_format():
    # Stripe signs "<timestamp>.<payload>" with HMAC-SHA256 and sends t=<timestamp>,v1=<hex>.
    header = sign_headers("stripe", "whsec_x", BODY, NOW)["Stripe-Signature"]
    t, v1 = header.split(",")
    assert t == f"t={int(NOW)}" and v1 == "v1=" + hmac_hex("whsec_x", f"{int(NOW)}.".encode() + BODY)


def test_missing_headers_and_unknown_scheme():
    with pytest.raises(SignatureError, match="timestamp"):
        verify("hmac", "s", BODY, {}, NOW)
    with pytest.raises(SignatureError):
        verify("github", "s", BODY, {}, NOW)
    with pytest.raises(SignatureError, match="unknown scheme"):
        verify("md5", "s", BODY, {}, NOW)
