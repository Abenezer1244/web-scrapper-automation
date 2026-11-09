"""src/workers/privacy_requests.py: the web form's email outbox and the deadline alert.

Real database: the worker's session is bound to one outer transaction (create_savepoint,
the worker's commits release savepoints), rolled back at the end. The email provider
(Resend) is the one stand-in: its send call is captured, never made. Synthetic people only.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager

import pytest
import resend
from sqlalchemy import text
from sqlalchemy.orm import Session

import src.workers.privacy_requests as worker
from src.config import settings
from src.db.session import sync_engine
from src.utils.crypto import blind_index, encrypt_field


@pytest.fixture
def conn(monkeypatch):
    with sync_engine.connect() as c:
        outer = c.begin()
        c.execute(text("UPDATE privacy_email_outbox SET gave_up_at = now() "
                       "WHERE sent_at IS NULL AND gave_up_at IS NULL"))

        @contextmanager
        def _session():
            s = Session(bind=c, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        monkeypatch.setattr(worker, "system_sync_session", _session)
        monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_sent")
        try:
            yield c
        finally:
            outer.rollback()


@pytest.fixture
def sent(monkeypatch):
    outbox: list[dict] = []
    monkeypatch.setattr(resend.Emails, "send", lambda payload: outbox.append(payload))
    return outbox


def _submit(conn, kind: str = "opt_out", agent: dict | None = None,
            days: int = 45) -> tuple[str, str]:
    email = f"privacy_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"
    contact = {"email": email} | ({"agent": agent} if agent else {})
    to = agent["email"] if agent else email
    details = {"first_name": "Alex", "last_name": "Example",
               "property_address": "1 TEST ST, SEATTLE WA"}
    conn.execute(text(
        "SELECT privacy_web_submit(:k, :h, :c, :d, :a, now() + make_interval(days => :n), "
        "'ccpa-2026')"),
        {"k": kind, "h": blind_index(to), "c": encrypt_field(json.dumps(contact)),
         "d": encrypt_field(json.dumps(details)), "a": agent is not None, "n": days})
    rid = conn.execute(text("SELECT id FROM privacy_requests WHERE requester_email_hmac = :h"),
                       {"h": blind_index(to)}).scalar()
    return str(rid), to


def test_the_verification_email_carries_a_one_time_link_and_no_personal_detail(conn, sent):
    rid, to = _submit(conn)
    assert worker.send_privacy_outbox() == 1
    [mail] = sent
    assert mail["to"] == [to]
    body = mail["text"] + mail["html"]
    token = re.search(r"/privacy/request/confirm#t=([A-Za-z0-9_-]+)", mail["text"]).group(1)
    assert conn.execute(text("SELECT count(*) FROM privacy_request_tokens WHERE request_id = :r "
                             "AND token_hash = :h"),
                        {"r": rid, "h": hashlib.sha256(token.encode()).hexdigest()}).scalar() == 1
    assert "We will respond by" in mail["text"]
    for leaked in ("Alex", "Example", "TEST ST", "SEATTLE"):
        assert leaked not in body
    assert "—" not in body and "–" not in body       # no em or en dashes
    assert conn.execute(text("SELECT sent_at IS NOT NULL FROM privacy_email_outbox "
                             "WHERE request_id = :r"), {"r": rid}).scalar() is True
    assert worker.send_privacy_outbox() == 0                   # nothing else due


def test_an_agent_gets_a_receipt_without_a_link(conn, sent):
    rid, to = _submit(conn, "delete", agent={"name": "Pat Agent", "email":
                      f"agent_{uuid.uuid4().hex[:8]}@test.bridgeleads.io", "authority": "letter"})
    worker.send_privacy_outbox()
    [mail] = sent
    assert mail["to"] == [to] and "#t=" not in mail["text"]
    assert "on someone else's behalf" in mail["text"]
    assert conn.execute(text("SELECT count(*) FROM privacy_request_tokens WHERE request_id = :r"),
                        {"r": rid}).scalar() == 0


def test_a_failed_send_is_kept_and_backed_off(conn, monkeypatch):
    rid, _ = _submit(conn)

    def _down(payload):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(resend.Emails, "send", _down)
    assert worker.send_privacy_outbox() == 1
    row = conn.execute(text("SELECT sent_at, attempts, next_attempt_at > now() AS later "
                            "FROM privacy_email_outbox WHERE request_id = :r"), {"r": rid}).one()
    assert (row.sent_at, row.attempts, row.later) == (None, 1, True)


def test_without_an_email_key_nothing_is_sent_and_nothing_is_lost(conn, sent, monkeypatch):
    rid, _ = _submit(conn)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    assert worker.send_privacy_outbox() == 0
    assert sent == []
    assert conn.execute(text("SELECT sent_at FROM privacy_email_outbox WHERE request_id = :r"),
                        {"r": rid}).scalar() is None


def test_the_daily_alerts_list_requests_due_soon_by_id_only(conn, monkeypatch):
    urgent, to = _submit(conn, "opt_out", days=1)
    soon, _ = _submit(conn, "access", days=5)
    later, _ = _submit(conn, "delete", days=40)
    alerts = []
    import src.workers.ops_alerts as ops
    monkeypatch.setattr(ops, "send_ops_alert", lambda *a: alerts.append(a) or True)
    worker.privacy_request_deadlines()
    by_kind = {kind: body for kind, _key, _subject, body in alerts}
    assert set(by_kind) == {"privacy_deadline_urgent", "privacy_deadline"}
    assert urgent in by_kind["privacy_deadline_urgent"]
    assert soon not in by_kind["privacy_deadline_urgent"]
    assert urgent in by_kind["privacy_deadline"] and soon in by_kind["privacy_deadline"]
    assert later not in by_kind["privacy_deadline"]
    assert all(to not in body for body in by_kind.values())
