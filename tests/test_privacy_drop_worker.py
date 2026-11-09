"""src/workers/privacy_drop.py (the DROP pull) and scripts/drop_match_report.py.

Real database: the worker's and the report's sessions are bound to one outer transaction
(create_savepoint; their commits release savepoints), rolled back at the end. DROP is a
real HTTP server on loopback (tests/test_privacy_drop.py's DropServer) answering with the
spec's responses. The one stand-in is send_ops_alert, captured instead of sent.
Synthetic people and identifiers only.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

import src.workers.ops_alerts as ops_alerts
import src.workers.privacy_drop as worker
from src.config import settings
from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import sync_engine
from src.privacy import drop
from tests.test_privacy_drop import DropServer, _csv, _zip

_SPEC = importlib.util.spec_from_file_location(
    "drop_match_report", Path(__file__).resolve().parents[1] / "scripts" / "drop_match_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

_KEY = "drop-worker-test-key-not-real-0123"


def _b64sha(standardized: str) -> str:
    return base64.b64encode(hashlib.sha256(standardized.encode("utf-8")).digest()).decode()


def _id(n: int) -> str:
    return f"WorkerTst{n:03d}"


@pytest.fixture
def conn(monkeypatch):
    with sync_engine.connect() as c:
        outer = c.begin()
        # This test's pulls only: age any recorded pull out of the 7-day spacing.
        c.execute(text("UPDATE drop_downloads SET downloaded_at = downloaded_at "
                       "- interval '60 days'"))

        @contextmanager
        def _session():
            s = Session(bind=c, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        monkeypatch.setattr(worker, "system_sync_session", _session)
        monkeypatch.setattr(report, "system_sync_session", _session)
        c.session_factory = _session
        try:
            yield c
        finally:
            outer.rollback()


@pytest.fixture
def server():
    s = DropServer()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def enabled(monkeypatch, server):
    monkeypatch.setattr(settings, "DROP_ENABLED", True)
    monkeypatch.setattr(settings, "DROP_API_KEY", _KEY)
    monkeypatch.setattr(settings, "DROP_BASE_URL", server.url)
    return server


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(ops_alerts, "send_ops_alert",
                        lambda kind, key, subject, body: sent.append((kind, key, subject, body))
                        or True)
    return sent


def _drop_zip(name_prefix: str) -> bytes:
    return _zip({
        f"{name_prefix}_Email.csv": _csv("ID,Hash", [(_id(1), _b64sha("a@test.example")),
                                                     (_id(2), _b64sha("b@test.example"))]),
        f"{name_prefix}_Phone.csv": _csv("Id,Hash", [(_id(3), _b64sha("2065550100"))]),
        f"{name_prefix}_NDZ.csv": _csv("ID,Hash", [(_id(4), _b64sha("ndz"))]),
        f"{name_prefix}_Removed.csv": _csv("ID,Hash,ListType", [
            (_id(2), _b64sha("b@test.example"), "Email")]),
    })


def _serve_zip(server, prefix: str) -> str:
    name = f"{prefix}_DROP.zip"
    server.answer(200, _drop_zip(prefix), Content_Type="application/zip",
                  Content_Disposition=f'attachment; filename="{name}"')
    return name


def test_disabled_does_nothing(conn, server, monkeypatch, alerts):
    monkeypatch.setattr(settings, "DROP_ENABLED", False)
    monkeypatch.setattr(settings, "DROP_API_KEY", _KEY)
    monkeypatch.setattr(settings, "DROP_BASE_URL", server.url)
    assert worker.pull_drop_lists() == {"status": "disabled"}
    monkeypatch.setattr(settings, "DROP_ENABLED", True)
    monkeypatch.setattr(settings, "DROP_API_KEY", "")
    assert worker.pull_drop_lists() == {"status": "disabled"}
    assert server.seen == [] and alerts == []


def test_a_pull_records_the_lists_applies_removed_and_waits_a_week(conn, enabled, alerts):
    prefix = f"20261109_{uuid.uuid4().int % 10000:04d}"
    name = _serve_zip(enabled, prefix)
    out = worker.pull_drop_lists()
    assert out == {"status": "recorded", "lists": {"email": 2, "phone": 1, "removed": 1},
                   "ignored": {"NDZ": 1}, "inserted": 3, "removed_applied": 1}
    rows = conn.execute(text(
        "SELECT i.list_type, i.drop_id, i.removed_at IS NOT NULL AS gone FROM drop_identifiers i "
        "JOIN drop_downloads d ON d.id = i.download_id WHERE d.file_name = :n "
        "ORDER BY i.drop_id"), {"n": name}).all()
    assert [tuple(r) for r in rows] == [("email", _id(1), False), ("email", _id(2), True),
                                        ("phone", _id(3), False)]
    files = conn.execute(text("SELECT list_files FROM drop_downloads WHERE file_name = :n"),
                         {"n": name}).scalar()
    assert files["email"] == f"{prefix}_Email.csv"
    assert enabled.seen[0][2]["X-API-KEY"] == _KEY
    assert worker.pull_drop_lists() == {"status": "not_due"}
    assert len(enabled.seen) == 1 and alerts == []


def test_no_new_data_is_recorded_as_a_pull(conn, enabled, alerts):
    enabled.json(200, '{"message": "No new consumer request data or removed identifiers are '
                      'available since your last completed download."}')
    before = conn.execute(text("SELECT count(*) FROM drop_downloads")).scalar()
    assert worker.pull_drop_lists()["status"] == "no_new_data"
    assert conn.execute(text("SELECT count(*) FROM drop_downloads WHERE file_name IS NULL "
                             "AND downloaded_at > now() - interval '1 minute'")).scalar() >= 1
    assert conn.execute(text("SELECT count(*) FROM drop_downloads")).scalar() == before + 1
    assert alerts == []


def test_preparing_records_nothing_and_asks_again_next_run(conn, enabled, alerts):
    enabled.json(202, '{"message": "No download package is available yet."}', Retry_After="60")
    before = conn.execute(text("SELECT count(*) FROM drop_downloads")).scalar()
    assert worker.pull_drop_lists() == {"status": "preparing", "retry_after": 60}
    assert conn.execute(text("SELECT count(*) FROM drop_downloads")).scalar() == before
    # Only an old last pull turns a still-preparing package into an alert.
    assert all("overdue" in a[2] for a in alerts)


@pytest.mark.parametrize(("status", "error"), [(401, drop.DropAuthError),
                                               (409, drop.DropConflictError),
                                               (429, drop.DropRetryLaterError)])
def test_a_failed_pull_alerts_ops_without_identifiers_and_raises(conn, enabled, alerts,
                                                                 status, error):
    enabled.json(status, '{"message": "x"}')
    with pytest.raises(error):
        worker.pull_drop_lists()
    [(kind, _key, subject, body)] = alerts
    assert kind == "drop_pull" and error.__name__ in subject
    assert f"HTTP {status}" in body and "CalPrivacy" in body and "45 days" in body
    assert _KEY not in subject + body


def test_a_malformed_download_alerts_and_records_nothing(conn, enabled, alerts):
    secret = _b64sha("c@test.example")
    enabled.answer(200, _zip({"20261109_0001_Email.csv": _csv(
        "ID,Hash", [(_id(1), secret), (_id(1), _b64sha("other"))])}),
        Content_Type="application/zip")
    before = conn.execute(text("SELECT count(*) FROM drop_downloads")).scalar()
    with pytest.raises(drop.DropFormatError):
        worker.pull_drop_lists()
    assert conn.execute(text("SELECT count(*) FROM drop_downloads")).scalar() == before
    [(_kind, _key, subject, body)] = alerts
    assert "DropFormatError" in subject
    for leak in (secret, _id(1), _KEY):
        assert leak not in subject + body


# ── The read-only match report ─────────────────────────────────────────────────────
def _lead(s, uid: str, job: str, **contact) -> str:
    rid = str(uuid.uuid4())
    s.add(Result(id=rid, job_id=job, user_id=uid, party_name="DROPTEST ALEX",
                 property_address="1 TEST ST", dedup_hash=uuid.uuid4().hex,
                 is_duplicate=False, enrichment_data={}, **contact))
    s.flush()
    return rid


def test_the_match_report_prints_counts_only(conn, capsys):
    tag = uuid.uuid4().hex[:8]
    a, b = f"drop.{tag}+x@test.example", f"other{tag}@test.example"
    n2, n3, n4 = (f"{uuid.uuid4().int % 10000:04d}" for _ in range(3))
    with conn.session_factory() as s:
        uid = str(uuid.uuid4())
        s.add(User(id=uid, email=f"{uid}@example.test", password_hash="x",
                   email_hmac=uuid.uuid4().hex))
        s.flush()
        cfg = str(uuid.uuid4())
        s.add(ScraperConfig(id=cfg, user_id=uid, name="Drop", county="king", state="WA",
                            record_type="probate", fields=[], enrichment=[],
                            schedule={"frequency": "manual"}, deliver={"emails": []}))
        s.flush()
        job = str(uuid.uuid4())
        s.add(Job(id=job, user_id=uid, scraper_config_id=cfg, status="done", trigger="manual",
                  record_count=0, billed_count=0, retry_count=0))
        s.flush()
        # Lead 1: the email in a different case and spacing; lead 2: the same email in
        # the extras list and the phone formatted; lead 3: a phone in the extras only.
        _lead(s, uid, job, email=f" {a.upper()} ")
        _lead(s, uid, job, email=b, emails=[b, a], phone=f"+1 (206) 555-{n2}")
        _lead(s, uid, job, phones=[{"number": f"425.555.{n3}", "type": "Mobile"}])
        s.commit()
    name = f"20261109_{uuid.uuid4().int % 10000:04d}_DROP.zip"
    did = str(conn.execute(text("SELECT drop_begin_download(:f, '{}', '{}', '{}')"),
                           {"f": name}).scalar())
    add = text("SELECT drop_add_identifiers(CAST(:d AS uuid), :t, CAST(:i AS text[]), CAST(:h AS text[]))")
    conn.execute(add, {"d": did, "t": "email", "i": [_id(1), _id(2), _id(3)],
                       "h": [_b64sha(a), _b64sha(f"nobody{tag}@test.example"),
                             _b64sha(f"removed{tag}@test.example")]})
    conn.execute(add, {"d": did, "t": "phone", "i": [_id(4), _id(5), _id(6)],
                       "h": [_b64sha(f"206555{n2}"), _b64sha(f"425555{n3}"),
                             _b64sha(f"999555{n4}")]})
    conn.execute(text("SELECT drop_mark_removed('email', ARRAY[:i], ARRAY[:h])"),
                 {"i": _id(3), "h": _b64sha(f"removed{tag}@test.example")})

    with conn.session_factory() as s:
        counts = report.match_counts(s, did)
    assert counts == {
        "email": {"pending": 2, "matched": 1, "unmatched": 1, "leads_hit": 2},
        "phone": {"pending": 3, "matched": 2, "unmatched": 1, "leads_hit": 2},
    }
    assert report.main(["--download", did]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "email: pending=2 matched=1 unmatched=1 leads_hit=2",
        "phone: pending=3 matched=2 unmatched=1 leads_hit=2",
    ]
    assert conn.execute(text("SELECT count(*) FROM drop_identifiers WHERE download_id = :d "
                             "AND removed_at IS NULL"), {"d": did}).scalar() == 5  # unchanged
