"""Two silent-failure classes found while closing out the Test 8 follow-ups.

1. `_upsert_notice` refreshed EVERY mutable field ON CONFLICT, so a re-crawl that parsed
   the same notice but failed to read one field overwrote a good value with NULL.
2. `send_ops_alert` returned False silently when OPS_ALERT_EMAIL was unset — which it
   was, in production, for all 15 call sites. Nothing was emailed, logged, or stored, so
   a four-week King crawl outage left no trace anywhere and could not be explained after
   the fact.
"""
from datetime import date  # noqa: F401  (used in the SQL-construction test)

# ── 1. A re-crawl may improve a field, never blank it ────────────────────────────


def test_value_columns_are_coalesced_on_conflict():
    """A NULL from the parser means "this run could not read it", not "the notice
    stopped saying it" — trustees amend sale dates and amounts, they do not delete
    them."""
    from src.workers.nts_crawler import _COALESCE_ON_UPDATE

    for col in ("auction_date", "principal_owing", "parcel",
                "property_address_normalized", "grantor", "trustee"):
        assert col in _COALESCE_ON_UPDATE


def test_bookkeeping_columns_are_never_coalesced():
    """is_active must be able to go false (the expiry pass), fetched_at must always
    advance, and source_url/raw_hash must track the issue actually read last —
    coalescing any of those would freeze the cache's own bookkeeping."""
    from src.workers.nts_crawler import _COALESCE_ON_UPDATE, _NO_UPDATE

    for col in ("is_active", "fetched_at", "source_url", "raw_hash"):
        assert col not in _COALESCE_ON_UPDATE, f"{col} must not be coalesced"
    # and the identity columns stay out of the update set entirely
    for col in ("id", "source", "ts_number", "created_at"):
        assert col in _NO_UPDATE


def test_upsert_builds_a_coalesce_for_value_columns_only():
    """Pin the mechanism, not just the constant: the ON CONFLICT set must actually
    wrap the value columns in COALESCE and leave the rest as plain excluded refs."""
    import inspect

    from src.workers import nts_crawler as c

    src = inspect.getsource(c._upsert_notice)
    assert "_COALESCE_ON_UPDATE" in src
    assert "coalesce(stmt.excluded[c], getattr(model, c))" in src.replace("_func.", "")


def test_a_reparse_that_loses_an_amount_cannot_erase_it():
    """The production case, checked against the SQL actually emitted.

    Tacoma WA-26-1050840-BB held principal_owing NULL while the lead matched from it
    still carried 575,150.38 — and re-parsing its own page yields 575150.38, so the
    page never stopped saying "$575,150.38". We had simply stored a NULL over it.
    """
    from sqlalchemy.dialects import postgresql

    from src.db.models import NtsNotice
    from src.workers.nts_crawler import _upsert_notice

    captured = {}

    class _FakeDB:
        def execute(self, stmt, params=None):
            captured.setdefault("stmts", []).append(stmt)
            return None

    row = {
        "source": "tacoma_daily_index", "ts_number": "WA-26-1050840-BB",
        "county": "pierce", "state": "WA", "parcel": "0220104064",
        "auction_date": date(2026, 9, 4), "principal_owing": None,
        "grantor": "CN Foods LLC", "is_active": True, "source_url": "https://x/y",
    }
    _upsert_notice(_FakeDB(), NtsNotice, row)
    sql = str(captured["stmts"][0].compile(dialect=postgresql.dialect()))

    # the amount is COALESCEd against the stored value; a NULL re-parse keeps 575,150.38
    assert "coalesce(excluded.principal_owing, nts_notices.principal_owing)" in sql.lower()
    assert "coalesce(excluded.auction_date, nts_notices.auction_date)" in sql.lower()
    # bookkeeping is NOT coalesced — is_active must still be able to go false
    assert "coalesce(excluded.is_active" not in sql.lower()
    assert "coalesce(excluded.source_url" not in sql.lower()


# ── 2. An alert nobody can reconstruct afterwards is not an alert ────────────────
#
# These are BEHAVIOURAL. The first round asserted against source text, which would not
# have caught a wrong column value, a write on the caller's thread, or the cooldown
# swallowing an occurrence (Codex).


def _fake_session(sink):
    """Stand-in for the persistence session; records what was added."""
    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def add(self, obj):
            sink.append(obj)

        def commit(self):
            pass

    return lambda: _S()


def _drain_persist_pool():
    """Block until the background persistence worker has finished its queue."""
    from src.workers import ops_alerts

    ops_alerts._persist_pool.submit(lambda: None).result(timeout=10)


def test_an_undelivered_alert_still_writes_a_row(monkeypatch):
    """Production's exact configuration — OPS_ALERT_EMAIL blank — used to return False
    and leave no trace anywhere. It must now leave a row."""
    from src.config import settings
    from src.workers import ops_alerts

    added = []
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    monkeypatch.setattr(ops_alerts, "_session_for_persist", _fake_session(added))

    assert ops_alerts.send_ops_alert("canary", "king/WA", "crawler barren", "body") is False
    _drain_persist_pool()

    assert len(added) == 1
    row = added[0]
    assert row.event == "ops_alert"
    assert row.user_id is None, "system-written, not tenant-scoped"
    assert row.path == "canary:king/WA"
    assert row.detail == "[undelivered] crawler barren"
    assert row.created_at is not None, "client-side created_at avoids INSERT..RETURNING"


def test_a_delivered_alert_is_recorded_as_sent(monkeypatch):
    """"Fired daily for four weeks and never delivered" is a different fact from "fired
    once and was e-mailed"."""
    import sys
    import types

    from src.config import settings
    from src.workers import ops_alerts

    added = []
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.invalid")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda kind, key: True)
    monkeypatch.setattr(ops_alerts, "_session_for_persist", _fake_session(added))

    fake = types.ModuleType("resend")
    fake.api_key = None
    fake.Emails = type("E", (), {"send": staticmethod(lambda _p: None)})
    monkeypatch.setitem(sys.modules, "resend", fake)

    assert ops_alerts.send_ops_alert("nts_crawl_barren", "queen_anne_news", "subj", "b") is True
    _drain_persist_pool()
    assert added[0].detail == "[sent] subj"


def test_a_cooldown_suppressed_alert_is_still_recorded(monkeypatch):
    """Recording every OCCURRENCE, not every e-mail, is what makes "this fired daily for
    four weeks" answerable — the 6h cooldown must not hide the pattern."""
    from src.config import settings
    from src.workers import ops_alerts

    added = []
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.invalid")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda kind, key: False)
    monkeypatch.setattr(ops_alerts, "_session_for_persist", _fake_session(added))

    assert ops_alerts.send_ops_alert("canary", "pierce/WA", "s", "b") is False
    _drain_persist_pool()
    assert len(added) == 1 and added[0].detail.startswith("[undelivered]")


def test_recording_never_runs_on_the_callers_thread(monkeypatch):
    """The point of the hand-off. In production OPS_ALERT_EMAIL is blank, so the e-mail
    send is never reached — inline persistence would have ADDED database work to a path
    that previously returned instantly, including a Stripe webhook handler, with the sync
    engine allowing pool_timeout=30s / connect_timeout=10s / statement_timeout=120s."""
    import threading

    from src.config import settings
    from src.workers import ops_alerts

    seen = {}

    class _S:
        def __enter__(self):
            seen["thread"] = threading.current_thread().name
            return self

        def __exit__(self, *_a):
            return False

        def add(self, _obj):
            pass

        def commit(self):
            pass

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    monkeypatch.setattr(ops_alerts, "_session_for_persist", lambda: _S())
    caller = threading.current_thread().name

    ops_alerts.send_ops_alert("k", "v", "s", "b")
    _drain_persist_pool()

    assert seen["thread"] != caller
    assert seen["thread"].startswith("ops-alert-persist")


def test_a_wedged_database_cannot_grow_the_queue_without_limit():
    """A stalled database must not let alerts pile up unbounded — the incident being
    reported may BE the database."""
    from src.workers import ops_alerts

    assert 0 < ops_alerts._PERSIST_MAX_BACKLOG <= 500
    assert ops_alerts._persist_pool._max_workers == 1, "one writer, not a thread per alert"


def test_persistence_failure_never_reaches_the_caller(monkeypatch):
    """send_ops_alert is called from a Stripe webhook handler; durability is additive and
    must never turn into an exception the caller sees."""
    from src.config import settings
    from src.workers import ops_alerts

    def _explode():
        raise RuntimeError("database is down")

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    monkeypatch.setattr(ops_alerts, "_session_for_persist", _explode)

    assert ops_alerts.send_ops_alert("k", "v", "s", "b") is False
    _drain_persist_pool()  # the worker swallowed it and the pool is still usable


def test_unconfigured_alerting_warns_instead_of_returning_silently(caplog):
    """The likelier misconfiguration used to be the silent one: a missing RESEND_API_KEY
    warned, a missing OPS_ALERT_EMAIL said nothing at all."""
    import logging

    from src.config import settings
    from src.workers import ops_alerts

    assert settings.OPS_ALERT_EMAIL == "", "CI default: alerting unconfigured"
    with caplog.at_level(logging.WARNING, logger="worker.ops_alerts"):
        ops_alerts.send_ops_alert("canary", "king/WA", "a barren crawl", "body")
    _drain_persist_pool()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("OPS_ALERT_EMAIL not configured" in m for m in warnings), warnings
    # and it names the alert it dropped, so the log line is actionable
    assert any("canary:king/WA" in m and "a barren crawl" in m for m in warnings), warnings
