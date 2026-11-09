"""POST /privacy/requests and /privacy/requests/confirm (Phase 6 P8).

Real Postgres + Redis. Requests the API commits are deleted in `finally` by their
address's blind index (tokens, outbox, events and payloads go with them: ON DELETE
CASCADE). Synthetic people only.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid

import pytest
from sqlalchemy import text

from src.config import settings
from src.utils.crypto import blind_index, decrypt_field


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "PRIVACY_INTAKE_ENABLED", True)


def _email() -> str:
    return f"privacy_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"


def _form(email: str, **over) -> dict:
    form = {"kind": "opt_out", "email": email, "first_name": "Alex", "last_name": "Example",
            "property_address": "1 TEST ST, SEATTLE WA"}
    return form | over


async def _requests(db, email: str) -> list:
    return (await db.execute(text(
        "SELECT id, kind, channel, deadline_rules, deadline_at > received_at AS later "
        "FROM privacy_requests WHERE requester_email_hmac = :h ORDER BY received_at"),
        {"h": blind_index(email)})).all()


async def _events(db, rid) -> list[str]:
    return list((await db.execute(text(
        "SELECT event_type FROM privacy_request_events WHERE request_id = :r ORDER BY seq"),
        {"r": rid})).scalars())


async def _cleanup(db, *emails: str) -> None:
    for email in emails:
        await db.execute(text("DELETE FROM privacy_requests WHERE requester_email_hmac = :h"),
                         {"h": blind_index(email)})
    await db.commit()


async def test_the_form_is_off_until_enabled_whatever_is_sent(client):
    for body in (_form(_email()), {"garbage": True}, {}):
        assert (await client.post("/privacy/requests", json=body)).status_code == 404
    assert (await client.post("/privacy/requests/confirm",
                              json={"token": "x"})).status_code == 404
    assert (await client.post("/privacy/requests/confirm", json={})).status_code == 404


async def test_a_submission_is_recorded_as_a_received_unverified_request(client, db, enabled):
    email = _email()
    try:
        r = await client.post("/privacy/requests", json=_form(email))
        assert (r.status_code, r.json()) == (202, {"status": "received"})
        [row] = await _requests(db, email)
        assert (row.kind, row.channel, row.deadline_rules, row.later) == (
            "opt_out", "web", "ccpa-2026", True)
        assert await _events(db, row.id) == ["received", "verification_sent"]
        payload = (await db.execute(text(
            "SELECT contact_encrypted, details_encrypted FROM privacy_request_payloads "
            "WHERE request_id = :r"), {"r": row.id})).one()
        assert json.loads(decrypt_field(payload.contact_encrypted)) == {"email": email}
        details = json.loads(decrypt_field(payload.details_encrypted))
        assert details["last_name"] == "Example" and "email" not in details
    finally:
        await _cleanup(db, email)


async def test_a_repeat_is_answered_identically_records_nothing_and_costs_nothing(
        client, db, enabled):
    email = _email()
    try:
        # More repeats than the per-address quota (3 a day): a repeat sends nothing,
        # so it never spends it, and every answer is the same.
        answers = [await client.post("/privacy/requests", json=_form(email)) for _ in range(5)]
        assert {(a.status_code, json.dumps(a.json())) for a in answers} == {
            (202, json.dumps({"status": "received"}))}
        assert len(await _requests(db, email)) == 1
    finally:
        await _cleanup(db, email)


async def test_an_agent_gets_the_email_and_the_request_goes_to_review(client, db, enabled):
    person, agent = _email(), _email()
    try:
        r = await client.post("/privacy/requests", json=_form(
            person, kind="delete",
            agent={"name": "Pat Agent", "email": agent, "authority": "signed letter attached"}))
        assert r.status_code == 202
        assert await _requests(db, person) == []            # mail goes to the agent
        [row] = await _requests(db, agent)
        assert await _events(db, row.id) == ["received", "in_review"]
    finally:
        await _cleanup(db, person, agent)


@pytest.mark.parametrize("over", [
    {"property_address": None},                       # no property hint at all
    {"kind": "erase"},
    {"email": "not-an-address"},
    {"details": "x" * 2001},
    {"unexpected": "field"},
])
async def test_invalid_forms_are_refused_before_anything_is_written(client, db, enabled, over):
    email = _email()
    form = {k: v for k, v in (_form(email) | over).items() if v is not None}
    try:
        assert (await client.post("/privacy/requests", json=form)).status_code == 422
        assert await _requests(db, email) == []
    finally:
        await _cleanup(db, email)


async def test_one_address_cannot_be_mailed_more_than_three_times_a_day(client, db, enabled):
    email = _email()
    try:
        codes = [(await client.post("/privacy/requests", json=_form(email, kind=k))).status_code
                 for k in ("opt_out", "delete", "access", "correct")]
        assert codes == [202, 202, 202, 429]
    finally:
        await _cleanup(db, email)


async def test_confirm_says_the_same_thing_for_every_token(client, db, enabled):
    email = _email()
    try:
        await client.post("/privacy/requests", json=_form(email))
        [row] = await _requests(db, email)
        outbox = (await db.execute(text(
            "SELECT id FROM privacy_email_outbox WHERE request_id = :r"), {"r": row.id})).scalar()
        token = secrets.token_urlsafe(32)
        await db.execute(text("SELECT privacy_outbox_sent(:o, :h, now() + interval '7 days')"),
                         {"o": outbox, "h": hashlib.sha256(token.encode()).hexdigest()})
        await db.commit()
        answers = [await client.post("/privacy/requests/confirm", json={"token": t})
                   for t in ("garbage", token, token)]
        assert {(a.status_code, json.dumps(a.json())) for a in answers} == {
            (200, json.dumps({"status": "ok"}))}
        assert await _events(db, row.id) == ["received", "verification_sent", "verified"]
    finally:
        await _cleanup(db, email)


async def test_without_redis_the_form_refuses_rather_than_running_unlimited(
        client, db, enabled, monkeypatch):
    import importlib

    import redis.exceptions as redis_exceptions

    # By module path: src.api.middleware re-exports a FUNCTION named rate_limit.
    rl = importlib.import_module("src.api.middleware.rate_limit")

    class _Down:
        def pipeline(self):
            return self

        def __getattr__(self, name):
            return lambda *a, **k: self

        async def execute(self):
            raise redis_exceptions.ConnectionError("down")

    monkeypatch.setattr(rl, "_get_redis", lambda: _Down())
    email = _email()
    try:
        r = await client.post("/privacy/requests", json=_form(email))
        assert r.status_code == 503 and r.headers.get("retry-after")
        assert await _requests(db, email) == []
    finally:
        await _cleanup(db, email)
