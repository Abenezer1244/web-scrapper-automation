"""The /auth/export routes (account data export P4c): request, status, link, download.

Real DB, real Redis, real endpoints and real login sessions. Only the R2 read is a
stand-in (app.dependency_overrides on the store dependency): R2 is an external service.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pyotp
import pytest
from httpx import AsyncClient
from main import app
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.download_tokens import mint_account_export_token, mint_download_token
from src.api.middleware import security as _security
from src.api.routes.auth import _account_export_store
from src.config import settings
from src.db.models import AuditEvent, User
from src.db.session import sync_engine
from src.utils.crypto import encrypt_field
from src.workers.account_export import export_key

_PW = "TestPass123!"
_ZIP = b"PK\x03\x04 a real zip would be here"


class Store:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.read: list[str] = []

    def stream(self, key):
        if self.fail:
            raise RuntimeError("r2 down")
        self.read.append(key)
        return iter([_ZIP[:10], _ZIP[10:]])


@pytest.fixture
def store():
    s = Store()
    app.dependency_overrides[_account_export_store] = lambda: s
    yield s
    app.dependency_overrides.pop(_account_export_store, None)


@pytest.fixture
def export_on(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_EXPORT_ENABLED", True)


async def _session(client: AsyncClient, user: User) -> dict[str, str]:
    r = await client.post("/auth/login", json={"email": user.email, "password": _PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _ask(client, auth, *, password=_PW, mfa_code=None):
    body = {"current_password": password}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return await client.post("/auth/export", json=body, headers=auth)


def _ready(user_id: str, *, expired: bool = False, ago: str = "0 seconds") -> str:
    """A finished export row (what the worker leaves), returning its id. `expired`:
    ready 8 days ago. `ago`: when it was asked for."""
    with sync_engine.begin() as c:
        eid = c.execute(text("INSERT INTO account_exports (user_id) VALUES (:u) RETURNING id"),
                        {"u": user_id}).scalar()
        c.execute(text(
            "UPDATE account_exports SET status = 'ready', size_bytes = :s, "
            "requested_at = now() - CAST(:ago AS interval), "
            "ready_at = now() - CASE WHEN :x THEN interval '8 days' ELSE interval '0 s' END, "
            "expires_at = now() - CASE WHEN :x THEN interval '8 days' ELSE interval '0 s' END "
            "+ interval '7 days' WHERE id = :i"),
            {"s": len(_ZIP), "ago": ago, "x": expired, "i": eid})
    return str(eid)


async def _events(db: AsyncSession, user: User, event: str) -> int:
    await asyncio.gather(*list(_security._audit_tasks))
    return len((await db.execute(
        select(AuditEvent.id).where(AuditEvent.user_id == user.id, AuditEvent.event == event)
    )).all())


def _pending_deletion(uid: str) -> None:
    with sync_engine.begin() as c:
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text("UPDATE users SET deletion_state = 'pending' WHERE id = :u"), {"u": uid})


# ── The flag ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_route_is_off_until_enabled(client, starter_user, store) -> None:
    auth = await _session(client, starter_user)
    eid = _ready(str(starter_user.id))
    token = mint_account_export_token(str(starter_user.id), eid, 60)
    assert (await _ask(client, auth)).status_code == 404
    assert (await client.get("/auth/export", headers=auth)).status_code == 404
    assert (await client.get(f"/auth/export/{eid}/url", headers=auth)).status_code == 404
    assert (await client.get(f"/auth/export/{eid}/download?token={token}")).status_code == 404
    assert store.read == []


# ── Asking for one ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ask_queues_one_export_and_audits_it(
        client, db, starter_user, export_on) -> None:
    auth = await _session(client, starter_user)
    assert (await client.get("/auth/export", headers=auth)).json() is None
    r = await _ask(client, auth)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending" and body["ready_at"] is None
    assert (await client.get("/auth/export", headers=auth)).json()["id"] == body["id"]
    assert await _events(db, starter_user, "account_export_requested") == 1
    # One in progress at a time, then one a day; a failed one does not count.
    assert (await _ask(client, auth)).status_code == 409
    with sync_engine.begin() as c:
        c.execute(text("UPDATE account_exports SET status = 'failed', "
                       "last_error = 'build_failed' WHERE id = :i"), {"i": body["id"]})
    again = await _ask(client, auth)
    assert again.status_code == 202
    with sync_engine.begin() as c:
        c.execute(text("UPDATE account_exports SET status = 'ready', size_bytes = 1, "
                       "ready_at = now(), expires_at = now() + interval '7 days' "
                       "WHERE id = :i"), {"i": again.json()["id"]})
    limited = await _ask(client, auth)
    assert limited.status_code == 429
    assert 86000 < int(limited.headers["Retry-After"]) <= 86401
    latest = (await client.get("/auth/export", headers=auth)).json()
    assert latest["status"] == "ready" and latest["next_allowed_at"] > latest["requested_at"]


@pytest.mark.asyncio
async def test_a_day_later_another_may_be_asked_for(client, starter_user, export_on) -> None:
    auth = await _session(client, starter_user)
    _ready(str(starter_user.id), ago="25 hours")
    assert (await _ask(client, auth)).status_code == 202


@pytest.mark.asyncio
async def test_the_password_and_second_factor_are_required(
        client, db, starter_user, export_on) -> None:
    auth = await _session(client, starter_user)
    assert (await _ask(client, auth, password="wrong-password")).status_code == 400
    secret = pyotp.random_base32()
    await db.execute(update(User).where(User.id == starter_user.id).values(
        mfa_enabled=True, mfa_secret_encrypted=encrypt_field(secret)))
    await db.commit()
    assert (await _ask(client, auth)).status_code == 400
    assert (await _ask(client, auth, mfa_code="000000")).status_code == 400
    assert (await client.get("/auth/export", headers=auth)).json() is None
    ok = await _ask(client, auth, mfa_code=pyotp.TOTP(secret).now())
    assert ok.status_code == 202, ok.text


@pytest.mark.asyncio
async def test_an_account_scheduled_for_deletion_cannot_ask_or_look(
        client, starter_user, export_on) -> None:
    auth = await _session(client, starter_user)
    _pending_deletion(str(starter_user.id))
    assert (await _ask(client, auth)).status_code == 403
    assert (await client.get("/auth/export", headers=auth)).status_code == 403


# ── Link + download ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_ready_export_downloads_through_its_link(
        client, db, starter_user, export_on, store) -> None:
    auth = await _session(client, starter_user)
    eid = _ready(str(starter_user.id))
    r = await client.get(f"/auth/export/{eid}/url", headers=auth)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    dl = await client.get(r.json()["url"])
    assert dl.status_code == 200, dl.text
    assert dl.content == _ZIP and dl.headers["content-type"] == "application/zip"
    assert dl.headers["cache-control"] == "no-store"
    assert "attachment" in dl.headers["content-disposition"]
    assert store.read == [export_key(str(starter_user.id), eid)]
    assert await _events(db, starter_user, "account_export_downloaded") == 1


@pytest.mark.asyncio
async def test_no_link_unless_ready_unexpired_and_yours(
        client, starter_user, business_user, export_on) -> None:
    auth = await _session(client, starter_user)
    mine_expired = _ready(str(starter_user.id), expired=True)
    theirs = _ready(str(business_user.id))
    with sync_engine.begin() as c:
        pending = str(c.execute(text("INSERT INTO account_exports (user_id) VALUES (:u) "
                                     "RETURNING id"), {"u": business_user.id}).scalar())
    for eid in (mine_expired, theirs, pending, str(uuid.uuid4()), "not-a-uuid"):
        assert (await client.get(f"/auth/export/{eid}/url", headers=auth)).status_code == 404, eid


@pytest.mark.asyncio
async def test_the_download_token_is_strict(
        client, starter_user, business_user, export_on, store) -> None:
    uid, other = str(starter_user.id), str(business_user.id)
    eid, theirs = _ready(uid), _ready(other)
    base = f"/auth/export/{eid}/download?token="
    assert (await client.get(base)).status_code == 401
    assert (await client.get(base + "garbage")).status_code == 401
    # A job download token for the same id never opens an export.
    assert (await client.get(base + mint_download_token(uid, eid, 60))).status_code == 401
    # Expired.
    assert (await client.get(base + mint_account_export_token(uid, eid, -1))).status_code == 401
    # Minted for another export.
    assert (await client.get(base + mint_account_export_token(uid, theirs, 60))).status_code == 403
    # Another account's token naming this export: the row is not theirs.
    assert (await client.get(base + mint_account_export_token(other, eid, 60))).status_code == 404
    # A malformed id is a 404 before any credential check.
    assert (await client.get("/auth/export/nope/download?token=x")).status_code == 404
    assert store.read == []


@pytest.mark.asyncio
async def test_logout_all_and_a_deletion_request_kill_issued_links(
        client, starter_user, business_user, export_on, store) -> None:
    uid = str(starter_user.id)
    eid = _ready(uid)
    token = mint_account_export_token(uid, eid, 3600)
    time.sleep(1.1)  # iat has second resolution; the cutoff must be later
    auth = await _session(client, starter_user)
    assert (await client.post("/auth/logout-all", headers=auth)).status_code == 204
    assert (await client.get(f"/auth/export/{eid}/download?token={token}")).status_code == 401

    other = str(business_user.id)
    theirs = _ready(other)
    fresh = mint_account_export_token(other, theirs, 3600)
    _pending_deletion(other)  # decision A: no download while scheduled for deletion
    assert (await client.get(f"/auth/export/{theirs}/download?token={fresh}")).status_code == 401
    assert store.read == []


@pytest.mark.asyncio
async def test_an_expired_export_never_downloads(client, starter_user, export_on, store) -> None:
    uid = str(starter_user.id)
    eid = _ready(uid, expired=True)
    token = mint_account_export_token(uid, eid, 60)
    assert (await client.get(f"/auth/export/{eid}/download?token={token}")).status_code == 404
    assert store.read == []


@pytest.mark.asyncio
async def test_a_storage_failure_is_a_503_not_a_broken_file(
        client, starter_user, export_on, store) -> None:
    store.fail = True
    uid = str(starter_user.id)
    eid = _ready(uid)
    token = mint_account_export_token(uid, eid, 60)
    assert (await client.get(f"/auth/export/{eid}/download?token={token}")).status_code == 503
