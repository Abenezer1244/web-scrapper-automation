"""Commercial-email unsubscribe: tokens, links and the /email/unsubscribe pages.

Real Postgres + Redis (no mocks). Compliance audit 2026-11, Phase 4b (CAN-SPAM).
Suppression rows created here are removed in `finally` (the table is append-only
for the runtime roles; the test session is the table owner).
"""

import time
import uuid

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from src.api.unsubscribe_tokens import (
    mint_unsubscribe_token,
    public_api_base,
    unsubscribe_url,
    verify_unsubscribe_token,
)
from src.config import settings
from src.utils.crypto import blind_index


def _email() -> str:
    return f"unsub_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"


async def _rows(db, email: str) -> list:
    return (await db.execute(text(
        "SELECT source FROM marketing_email_suppressions WHERE email_hmac = :h"
    ), {"h": blind_index(email)})).all()


async def _cleanup(db, email: str) -> None:
    await db.execute(text("DELETE FROM marketing_email_suppressions WHERE email_hmac = :h"),
                     {"h": blind_index(email)})
    await db.commit()


# ─── Tokens and links ─────────────────────────────────────────────────────────

def test_a_token_names_the_address_by_hmac_only():
    email = _email()
    token = mint_unsubscribe_token(email)
    assert verify_unsubscribe_token(token) == blind_index(email)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert email not in str(claims) and "sub" not in claims


@pytest.mark.parametrize("claims_patch", [
    {"aud": "bridgeleads-download"},   # another token family
    {"purpose": "download"},
    {"exp": int(time.time()) - 10},    # expired
    {"ehm": "not-a-hash"},
])
def test_other_or_broken_tokens_are_refused(claims_patch):
    now = int(time.time())
    claims = {"purpose": "unsubscribe", "ehm": "a" * 64, "aud": "bridgeleads-unsubscribe",
              "iss": "bridgeleads", "iat": now, "exp": now + 60, **claims_patch}
    assert verify_unsubscribe_token(jwt.encode(claims, settings.SECRET_KEY, "HS256")) is None


def test_a_forged_or_empty_token_is_refused():
    now = int(time.time())
    forged = jwt.encode({"purpose": "unsubscribe", "ehm": "a" * 64, "aud": "bridgeleads-unsubscribe",
                         "iss": "bridgeleads", "iat": now, "exp": now + 60}, "x" * 40, "HS256")
    assert verify_unsubscribe_token(forged) is None
    assert verify_unsubscribe_token("") is None
    assert verify_unsubscribe_token("x" * 5000) is None


@pytest.mark.parametrize(("base", "usable"), [
    ("https://api.bridgeleads.io", True),
    ("https://api.bridgeleads.io/", True),
    ("http://localhost:8000", True),
    ("", False),
    ("http://api.bridgeleads.io", False),           # plaintext, not local
    ("https://api.bridgeleads.io?x=1", False),
    ("https://api.bridgeleads.io\r\nX-Evil: 1", False),  # header injection
    ("javascript:alert(1)", False),
    ("https://api.bridgeleads.io/wrong", False),  # a path: links would miss the route
    ("https://api.bridgeleads.io:notaport", False),
    ("https://user:pw@api.bridgeleads.io", False),
    ("https://:secret@api.bridgeleads.io", False),   # password-only userinfo
    ("https://[::1", False),                          # malformed host: no exception
])
def test_only_a_safe_absolute_base_builds_links(monkeypatch, base, usable):
    monkeypatch.setattr(settings, "API_BASE_URL", base)
    assert (public_api_base() is not None) is usable
    url = unsubscribe_url(_email())
    assert (url is not None) is usable
    if usable:
        assert url.startswith(base.rstrip("/") + "/email/unsubscribe?token=")


# ─── Pages ────────────────────────────────────────────────────────────────────

async def test_get_shows_a_confirm_page_and_changes_nothing(client: AsyncClient, db):
    email = _email()
    token = mint_unsubscribe_token(email)
    r = await client.get("/email/unsubscribe", params={"token": token})
    assert r.status_code == 200
    assert 'method="post"' in r.text and 'name="token"' in r.text
    assert 'action="/email/unsubscribe"' in r.text  # the POST URL carries no token
    assert r.headers["cache-control"] == "no-store"
    assert email not in r.text and blind_index(email) not in r.text
    assert await _rows(db, email) == []


async def test_confirm_button_records_the_opt_out_once(client: AsyncClient, db):
    email = _email()
    token = mint_unsubscribe_token(email)
    try:
        for _ in range(2):
            r = await client.post("/email/unsubscribe", data={"token": token})
            assert r.status_code == 200 and "unsubscribed" in r.text
        assert [row.source for row in await _rows(db, email)] == ["link"]
    finally:
        await _cleanup(db, email)


async def test_rfc8058_one_click_post_records_the_opt_out(client: AsyncClient, db):
    email = _email()
    token = mint_unsubscribe_token(email)
    try:
        r = await client.post("/email/unsubscribe", params={"token": token},
                              data={"List-Unsubscribe": "One-Click"})
        assert r.status_code == 200
        assert [row.source for row in await _rows(db, email)] == ["one_click"]
    finally:
        await _cleanup(db, email)


async def test_an_invalid_link_shows_one_generic_page_and_records_nothing(client: AsyncClient, db):
    for method, kwargs in (("get", {"params": {"token": "bad"}}),
                           ("post", {"data": {"token": "bad"}}),
                           ("post", {"params": {"token": "bad"},
                                     "data": {"List-Unsubscribe": "One-Click"}})):
        r = await getattr(client, method)("/email/unsubscribe", **kwargs)
        assert r.status_code == 400, (method, kwargs)
        assert "This unsubscribe link isn" in r.text  # apostrophe is HTML-escaped
    count = (await db.execute(text("SELECT count(*) FROM marketing_email_suppressions "
                                   "WHERE email_hmac = :h"), {"h": "b" * 64})).scalar()
    assert count == 0


def test_the_routes_are_not_in_the_public_api_schema():
    from main import app

    assert not any(p.startswith("/email") for p in app.openapi()["paths"])


async def test_pages_tell_the_browser_to_send_no_referrer(client: AsyncClient):
    # The middleware's Referrer-Policy header would still send the token-bearing URL
    # on the confirm form's same-origin POST; the meta tag overrides it.
    for r in (await client.get("/email/unsubscribe", params={"token": mint_unsubscribe_token(_email())}),
              await client.get("/email/unsubscribe", params={"token": "bad"})):
        assert '<meta name="referrer" content="no-referrer">' in r.text


async def test_an_oversized_body_is_refused_and_records_nothing(client: AsyncClient, db):
    email = _email()
    token = mint_unsubscribe_token(email)
    r = await client.post("/email/unsubscribe", params={"token": token},
                          content=b"List-Unsubscribe=One-Click&pad=" + b"x" * 8192,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400
    assert await _rows(db, email) == []


async def test_one_busy_sender_ip_never_turns_other_peoples_opt_outs_into_429s(
    client: AsyncClient, db
):
    # Mail providers POST one-click opt-outs from a few shared IPs. 65 different
    # addresses from ONE client IP exceed the general zone's 60/min per IP: with a
    # per-IP bucket the tail would get 429 instead of the 2xx RFC 8058 expects.
    emails = [_email() for _ in range(65)]
    try:
        for email in emails:
            r = await client.post("/email/unsubscribe",
                                  params={"token": mint_unsubscribe_token(email)},
                                  data={"List-Unsubscribe": "One-Click"})
            assert r.status_code == 200, r.status_code
    finally:
        for email in emails:
            await db.execute(text("DELETE FROM marketing_email_suppressions WHERE email_hmac = :h"),
                             {"h": blind_index(email)})
        await db.commit()


async def test_a_chunked_oversized_body_is_refused_without_content_length(client: AsyncClient, db):
    # A generator body is sent chunked, with no Content-Length to trust.
    email = _email()

    async def _chunks():
        yield b"List-Unsubscribe=One-Click&pad="
        for _ in range(10):
            yield b"x" * 1024

    r = await client.post("/email/unsubscribe", params={"token": mint_unsubscribe_token(email)},
                          content=_chunks(),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400
    assert await _rows(db, email) == []


async def test_one_click_accepts_a_multipart_body_as_rfc8058_allows(client: AsyncClient, db):
    email = _email()
    try:
        r = await client.post("/email/unsubscribe", params={"token": mint_unsubscribe_token(email)},
                              files={"List-Unsubscribe": (None, "One-Click")})
        assert r.status_code == 200
        assert [row.source for row in await _rows(db, email)] == ["one_click"]
    finally:
        await _cleanup(db, email)


async def test_the_confirm_form_asks_for_no_referrer(client: AsyncClient):
    r = await client.get("/email/unsubscribe", params={"token": mint_unsubscribe_token(_email())})
    assert 'rel="noreferrer"' in r.text
