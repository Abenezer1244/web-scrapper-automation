"""Audit #3 (2026-09-27): S3-07 download revocation, S3-09 missing rate limits.

S3-07. ``GET /jobs/{id}/download`` authenticated its own way instead of through
``get_auth_context``, and that copy never learned the session-family check added
for A-1/A-5. A signed-out session's access token (1 h lifetime) kept streaming
the full lead CSV, by header or by ``?token=``. The fix routes the header path
through ``get_auth_context`` and lets the query string carry only the
job-bound ``purpose=download`` token, never a session JWT.

S3-09. ``/download`` and ``/export-url`` (each rebuilds the CSV and decrypts PII),
job cancel and the scraper write routes had no limiter at all.
"""
from __future__ import annotations

import importlib
import uuid

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient

from src.api.download_tokens import mint_download_token
from src.db.models import Job, Result, ScraperConfig

# The package re-exports the rate_limit FUNCTION under the module name, so a plain
# `import ... as` would bind the function, not the module.
rl = importlib.import_module("src.api.middleware.rate_limit")

# The budgets the fix sets. Literal on purpose: on the unfixed code these tests must
# fail by getting no 429, not by a missing zone name.
EXPORT_BUDGET = 20  # per user per minute, /download + /export-url together
WRITES_BUDGET = 30  # per user per minute, cancel + scraper writes

_PW = "SecurePass1!"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _session_with_a_done_run(client: AsyncClient, db) -> tuple[dict, str, str]:
    """A real registered session (so its tokens carry a session family) owning one
    delivered run with one lead row."""
    email = f"s307_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    reg = await client.post("/auth/register", json={
        "first_name": "Dl", "last_name": "Test", "email": email, "password": _PW,
    })
    assert reg.status_code == 201, reg.text
    session = reg.json()
    me = await client.get("/auth/me", headers=_bearer(session["access_token"]))
    assert me.status_code == 200, me.text
    user_id = me.json()["id"]

    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, name="Pierce tax", county="pierce",
        state="WA", record_type="tax_delinquent", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.flush()
    job = Job(
        id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=config.id,
        status="done", trigger="manual", export_key=f"exports/{user_id}/run.csv",
    )
    db.add(job)
    await db.flush()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job.id, user_id=user_id,
        party_name="S307 OWNER", parcel_id="0123456789",
        property_address="1 Main St, Tacoma, WA 98402",
    ))
    await db.commit()
    return session, user_id, job.id


# ─── S3-07: revocation ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_live_session_downloads_by_header(client, db):
    # Positive control: without it every 401 below could be a broken fixture.
    session, _, job_id = await _session_with_a_done_run(client, db)
    dl = await client.get(f"/jobs/{job_id}/download", headers=_bearer(session["access_token"]))
    assert dl.status_code == 200 and "S307 OWNER" in dl.text


@pytest.mark.asyncio
async def test_a_logged_out_access_token_cannot_download_by_header(client, db):
    session, _, job_id = await _session_with_a_done_run(client, db)
    out = await client.post("/auth/logout", json={"refresh_token": session["refresh_token"]})
    assert out.status_code == 204
    # The same token is already refused elsewhere; /download must agree.
    assert (await client.get("/auth/me", headers=_bearer(session["access_token"]))).status_code == 401
    dl = await client.get(f"/jobs/{job_id}/download", headers=_bearer(session["access_token"]))
    assert dl.status_code == 401 and "S307 OWNER" not in dl.text


@pytest.mark.asyncio
async def test_a_session_jwt_is_never_accepted_in_the_query_string(client, db):
    session, _, job_id = await _session_with_a_done_run(client, db)
    # Even a LIVE session token: bearer credentials do not belong in URLs.
    dl = await client.get(f"/jobs/{job_id}/download", params={"token": session["access_token"]})
    assert dl.status_code == 401 and "S307 OWNER" not in dl.text


@pytest.mark.asyncio
async def test_a_download_token_still_works_in_the_query_string(client, db):
    # Control for the query path: the export-url flow and emailed links use this.
    session, user_id, job_id = await _session_with_a_done_run(client, db)
    url = await client.get(f"/jobs/{job_id}/export-url", headers=_bearer(session["access_token"]))
    assert url.status_code == 200
    dl = await client.get(url.json()["url"])
    assert dl.status_code == 200 and "S307 OWNER" in dl.text
    minted = mint_download_token(user_id, job_id, ttl_seconds=60)
    assert (await client.get(f"/jobs/{job_id}/download", params={"token": minted})).status_code == 200


@pytest.mark.asyncio
async def test_a_download_token_for_another_job_is_refused(client, db):
    session, user_id, job_id = await _session_with_a_done_run(client, db)
    other = mint_download_token(user_id, str(uuid.uuid4()), ttl_seconds=60)
    dl = await client.get(f"/jobs/{job_id}/download", params={"token": other})
    assert dl.status_code in (401, 403) and "S307 OWNER" not in dl.text


# ─── S3-09: rate limits ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_url_and_download_share_a_per_user_budget(client, db):
    session, _, job_id = await _session_with_a_done_run(client, db)
    auth = _bearer(session["access_token"])
    budget = EXPORT_BUDGET
    for i in range(budget):
        r = await client.get(f"/jobs/{job_id}/export-url", headers=auth)
        assert r.status_code == 200, f"request {i + 1} of {budget} was refused"
    assert (await client.get(f"/jobs/{job_id}/export-url", headers=auth)).status_code == 429
    # Shared bucket: the CSV rebuild is the expensive part, whichever route asks.
    assert (await client.get(f"/jobs/{job_id}/download", headers=auth)).status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/jobs/{id}", None),
    ("DELETE", "/scrapers/{id}", None),
    # A VALID body: FastAPI rejects a malformed one with 422 before the handler
    # runs, and a limiter inside the handler would never see it.
    ("PATCH", "/scrapers/{id}", {"updated_at": "2026-01-01T00:00:00Z", "name": "renamed"}),
])
async def test_cancel_and_scraper_writes_are_throttled_per_user(client, db, method, path, body):
    session, _, _ = await _session_with_a_done_run(client, db)
    auth = _bearer(session["access_token"])
    budget = WRITES_BUDGET
    url = path.format(id=uuid.uuid4())
    for _ in range(budget):
        r = await client.request(method, url, headers=auth, json=body)
        assert r.status_code == 404, r.text  # owner-scoped miss, not a validation error
    assert (await client.request(method, url, headers=auth, json=body)).status_code == 429


@pytest.mark.asyncio
async def test_the_export_zone_stays_throttled_when_redis_is_down(client, db, monkeypatch):
    """Codex (2a consult): a Redis outage must not open unlimited CSV rebuilds.
    A real client pointed at a closed port, not a stub."""
    session, _, job_id = await _session_with_a_done_run(client, db)
    auth = _bearer(session["access_token"])
    monkeypatch.setattr(rl, "_redis_client", aioredis.from_url("redis://127.0.0.1:1/0"))
    monkeypatch.setattr(rl, "_fallback_hits", {})
    budget = EXPORT_BUDGET
    for _ in range(budget):
        assert (await client.get(f"/jobs/{job_id}/export-url", headers=auth)).status_code == 200
    assert (await client.get(f"/jobs/{job_id}/export-url", headers=auth)).status_code == 429
