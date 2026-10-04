"""A malformed run id is 404 "Job not found" on every route that takes one, never a 500.

Contract: tasks/todo-lookup-contacts.md, "### A" of "## Phase 1 follow-ups A-C" as amended by
AW1-AW3 and AX1-AX4. `Job.id` is a uuid column, so a non-UUID path id used to reach Postgres as
a failed cast (a DataError the error handler does not class as an outage): a 500 for any
authenticated caller. The routes now canonicalize the id first (`_canonical_job_id`): malformed is
404, and any spelling of a real id resolves to that id.

Real PG and real Redis, through the API client. No mocks.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest

from src.api.download_tokens import mint_download_token
from src.config import settings
from tests.test_contact_lookup_quote import _auth, _job

_MALFORMED = ("not-a-uuid", "123", "0" * 37, "6a5c4ae2-0000-0000-0000-00000000000g")
_NOT_FOUND = {"detail": "Job not found"}


def _config_of(job_id: str) -> str:
    from src.db.models import Job
    from src.db.session import system_sync_session
    with system_sync_session() as db:
        return str(db.get(Job, job_id).scraper_config_id)


def _spellings(job_id: str) -> tuple[str, ...]:
    """Non-canonical spellings Python's UUID accepts for the same id."""
    u = uuid.UUID(job_id)
    return (job_id.upper(), u.hex, "{" + job_id + "}")


async def _call(client, route: str, job_id: str, *, token: str | None, config_id: str | None = None):
    headers = _auth(token) if token else {}
    if route == "get":
        return await client.get(f"/jobs/{job_id}", headers=headers)
    if route == "cancel":
        return await client.delete(f"/jobs/{job_id}", headers=headers)
    if route == "results":
        return await client.get(f"/jobs/{job_id}/results", headers=headers)
    if route == "logs":
        return await client.get(f"/jobs/{job_id}/logs", headers=headers)
    if route == "export_url":
        return await client.get(f"/jobs/{job_id}/export-url", headers=headers)
    if route == "download":
        return await client.get(f"/jobs/{job_id}/download", headers=headers)
    if route == "dialer_replay":
        return await client.post(f"/scrapers/{config_id or uuid.uuid4()}/jobs/{job_id}/dialer-replay",
                                 headers=headers)
    raise AssertionError(route)


_ROUTES = ("get", "cancel", "results", "logs", "export_url", "download", "dialer_replay")
# The answer each route gives for the caller's own finished run with no export: proof the run
# was FOUND (anything but "Job not found").
_FOUND = {
    "get": (200, None),
    "cancel": (400, "Cannot cancel a job in 'done' status"),
    "results": (200, None),
    "logs": (200, None),
    "export_url": (404, "No export available yet"),
    "download": (404, "No export available yet"),
    "dialer_replay": (200, None),
}


# ── malformed ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_malformed_run_id_is_404_for_an_authenticated_caller(client, starter_user, starter_token, route):
    config_id = _config_of(_job(starter_user.id))  # dialer-replay: a REAL config, only the run id is bad
    for bad in _MALFORMED:
        r = await _call(client, route, bad, token=starter_token, config_id=config_id)
        assert r.status_code == 404, (route, bad, r.status_code, r.text)
        assert r.json() == _NOT_FOUND, (route, bad)


async def test_dialer_replay_a_malformed_config_id_is_404(client, starter_user, starter_token):
    job_id = _job(starter_user.id)
    for bad in _MALFORMED:
        r = await client.post(f"/scrapers/{bad}/jobs/{job_id}/dialer-replay", headers=_auth(starter_token))
        assert r.status_code == 404, (bad, r.status_code, r.text)
        assert r.json() == _NOT_FOUND


@pytest.mark.parametrize("route", [r for r in _ROUTES if r != "download"])
async def test_unauthenticated_stays_401_whatever_the_id(client, route):
    """Auth runs before the handler (AW2 / AX3): canonicalizing changes nothing for a caller
    with no credentials."""
    r = await _call(client, route, "not-a-uuid", token=None)
    assert r.status_code == 401, (route, r.status_code)


async def test_download_canonicalizes_before_any_credential(client, starter_user, starter_token):
    """download takes its credential from the query or the header INSIDE the handler, so the
    id is checked first (AW2): malformed with no auth, a bearer, or a real download token are
    all the same 404, never a 401 / 403 / 500."""
    real = _job(starter_user.id)
    token = mint_download_token(starter_user.id, real)
    for bad in _MALFORMED:
        for kwargs in ({}, {"headers": _auth(starter_token)}, {"params": {"token": token}}):
            r = await client.get(f"/jobs/{bad}/download", **kwargs)
            assert r.status_code == 404, (bad, kwargs.keys(), r.status_code, r.text)
            assert r.json() == _NOT_FOUND


# ── any spelling of a real id resolves to it ─────────────────────────────────


@pytest.mark.parametrize("route", _ROUTES)
async def test_a_non_canonical_spelling_of_your_own_run_finds_it(client, starter_user, starter_token, route):
    job_id = _job(starter_user.id)
    config_id = _config_of(job_id)
    want_status, want_detail = _FOUND[route]
    for spelling in (job_id, *_spellings(job_id)):
        r = await _call(client, route, spelling, token=starter_token, config_id=config_id)
        assert r.status_code == want_status, (route, spelling, r.status_code, r.text)
        if want_detail is not None:
            assert r.json() == {"detail": want_detail}, (route, spelling)
        if route == "get":
            assert r.json()["id"] == job_id
        if route == "dialer_replay":
            assert r.json() == {"job_id": job_id, "replayed": 0}  # the canonical id (AX2)


async def test_dialer_replay_a_non_canonical_config_id_finds_the_run(client, starter_user, starter_token):
    job_id = _job(starter_user.id)
    config_id = _config_of(job_id)
    r = await client.post(f"/scrapers/{config_id.upper()}/jobs/{job_id}/dialer-replay",
                          headers=_auth(starter_token))
    assert r.status_code == 200, r.text
    assert r.json() == {"job_id": job_id, "replayed": 0}


# ── tenancy is unchanged ─────────────────────────────────────────────────────


@pytest.mark.parametrize("route", _ROUTES)
async def test_another_accounts_run_is_404_in_any_spelling(client, business_user, starter_token, route):
    theirs = _job(business_user.id)
    config_id = _config_of(theirs)
    for spelling in (theirs, *_spellings(theirs)):
        r = await _call(client, route, spelling, token=starter_token, config_id=config_id)
        assert r.status_code == 404, (route, spelling, r.status_code, r.text)
        assert r.json() == _NOT_FOUND, (route, spelling)


# ── the download token's job claim (AW3 / AX4) ───────────────────────────────


def _token_with(user_id: str, **claims) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "purpose": "download", "aud": "bridgeleads-download",
               "iss": "bridgeleads", "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60}
    payload.update(claims)
    return jwt.encode({k: v for k, v in payload.items() if v is not _ABSENT},
                      settings.SECRET_KEY, algorithm="HS256")


_ABSENT = object()


async def test_a_token_claim_in_another_spelling_of_the_run_is_accepted(client, starter_user):
    """An emailed link minted with a non-canonical spelling of the run still opens it."""
    job_id = _job(starter_user.id)
    for claim in _spellings(job_id):
        r = await client.get(f"/jobs/{job_id}/download", params={"token": _token_with(starter_user.id, job_id=claim)})
        assert r.status_code == 404 and r.json() == {"detail": "No export available yet"}, (claim, r.text)


@pytest.mark.parametrize("claim", [_ABSENT, None, 12345, ["x"], "not-a-uuid", ""])
async def test_a_bad_token_claim_is_the_existing_403(client, starter_user, claim):
    job_id = _job(starter_user.id)
    r = await client.get(f"/jobs/{job_id}/download", params={"token": _token_with(starter_user.id, job_id=claim)})
    assert r.status_code == 403, (claim, r.status_code, r.text)
    assert r.json() == {"detail": "Token not valid for this job"}


async def test_a_token_for_another_run_is_still_403(client, starter_user):
    a, b = _job(starter_user.id), _job(starter_user.id)
    r = await client.get(f"/jobs/{a}/download", params={"token": _token_with(starter_user.id, job_id=b.upper())})
    assert r.status_code == 403
    assert r.json() == {"detail": "Token not valid for this job"}
