"""client_ip() trusts the web app's BFF IP assertion only with the shared secret.

The web app's server-side login route calls the API from Vercel, so without the
assertion every user would share Vercel's egress IP in the auth rate limiter and
brute-force lock. A forged or malformed assertion must never pick the key.
"""
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from src.api.middleware.rate_limit import (
    BFF_CLIENT_IP_HEADER,
    BFF_SECRET_HEADER,
    client_ip,
    require_valid_bff_assertion,
)
from src.config import settings

SECRET = "s" * 40
OLD_SECRET = "o" * 40


def _request(headers: dict[str, str], peer: str = "203.0.113.9") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 443),
        }
    )


@pytest.fixture
def bff_secret(monkeypatch):
    monkeypatch.setattr(settings, "BFF_SHARED_SECRET", f"{SECRET},{OLD_SECRET}")


def test_valid_assertion_sets_the_client_ip(bff_secret):
    req = _request({BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "198.51.100.7"})
    assert client_ip(req) == "198.51.100.7"


def test_previous_secret_still_accepted_during_rotation(bff_secret):
    req = _request({BFF_SECRET_HEADER: OLD_SECRET, BFF_CLIENT_IP_HEADER: "198.51.100.7"})
    assert client_ip(req) == "198.51.100.7"


def test_ipv6_is_canonicalised(bff_secret):
    req = _request({BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "2001:DB8:0:0::1"})
    assert client_ip(req) == "2001:db8::1"


@pytest.mark.parametrize(
    "headers",
    [
        {BFF_SECRET_HEADER: "wrong" * 10, BFF_CLIENT_IP_HEADER: "198.51.100.7"},
        {BFF_SECRET_HEADER: "", BFF_CLIENT_IP_HEADER: "198.51.100.7"},
        {BFF_SECRET_HEADER: SECRET},
        {BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "198.51.100.7, 10.0.0.1"},
        {BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "198.51.100.7:443"},
        {BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "not-an-ip"},
    ],
)
def test_presented_but_invalid_assertion_is_rejected(bff_secret, headers):
    with pytest.raises(HTTPException) as exc:
        client_ip(_request(headers))
    assert exc.value.status_code == 401


def test_unconfigured_secret_never_trusts_an_assertion(monkeypatch):
    monkeypatch.setattr(settings, "BFF_SHARED_SECRET", "")
    with pytest.raises(HTTPException):
        client_ip(_request({BFF_SECRET_HEADER: "", BFF_CLIENT_IP_HEADER: "198.51.100.7"}))


def test_short_secret_is_never_trusted(monkeypatch):
    monkeypatch.setattr(settings, "BFF_SHARED_SECRET", "short")
    with pytest.raises(HTTPException):
        client_ip(_request({BFF_SECRET_HEADER: "short", BFF_CLIENT_IP_HEADER: "198.51.100.7"}))


def test_client_ip_header_alone_is_ignored(bff_secret):
    # Without the secret header, the assertion header is just browser input.
    req = _request({BFF_CLIENT_IP_HEADER: "198.51.100.7"})
    assert client_ip(req) == "203.0.113.9"


def test_validated_ip_is_reused_without_revalidating(bff_secret, monkeypatch):
    # audit_log() runs after a commit and calls client_ip(); once the app-wide
    # dependency has validated the assertion, that call must not raise even if
    # the secret were rotated out mid-request.
    req = _request({BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "198.51.100.7"})
    require_valid_bff_assertion(req)
    monkeypatch.setattr(settings, "BFF_SHARED_SECRET", "")
    assert client_ip(req) == "198.51.100.7"


async def test_app_rejects_forged_assertion_before_any_endpoint(bff_secret):
    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        forged = await c.get(
            "/health", headers={BFF_SECRET_HEADER: "x" * 40, BFF_CLIENT_IP_HEADER: "198.51.100.7"}
        )
        valid = await c.get(
            "/health", headers={BFF_SECRET_HEADER: SECRET, BFF_CLIENT_IP_HEADER: "198.51.100.7"}
        )
        plain = await c.get("/health")
    assert forged.status_code == 401
    assert valid.status_code == 200
    assert plain.status_code == 200
