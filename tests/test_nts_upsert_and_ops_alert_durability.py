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


def test_every_ops_alert_is_recorded_whatever_the_delivery_outcome():
    """Recording must not be conditional on the delivery path working — that path is
    exactly what was broken. A `finally` makes it unconditional across every early
    return AND the except branch."""
    import inspect

    from src.workers import ops_alerts

    src = inspect.getsource(ops_alerts.send_ops_alert)
    tail = src[src.index("finally:"):]
    assert "_persist_ops_alert(kind, key, subject, delivered)" in tail
    # exactly one call site, in the finally — not duplicated on some paths
    assert src.count("_persist_ops_alert(") == 1


def test_recording_does_not_sit_in_front_of_the_alert_email():
    """Persisting BEFORE delivery would put a database write ahead of every alert, so a
    slow or unreachable database would delay the very message reporting trouble."""
    import inspect

    from src.workers import ops_alerts

    src = inspect.getsource(ops_alerts.send_ops_alert)
    assert src.index("_persist_ops_alert(") > src.index("resend.Emails.send")


def test_the_recorded_row_says_whether_it_was_delivered():
    """"Fired daily for four weeks and never delivered" is a different fact from "fired
    once and was e-mailed" — only the row can tell them apart afterwards."""
    import inspect

    from src.workers import ops_alerts

    send = inspect.getsource(ops_alerts.send_ops_alert)
    assert "delivered = False" in send and "delivered = True" in send
    persist = inspect.getsource(ops_alerts._persist_ops_alert)
    assert "'sent' if delivered else 'undelivered'" in persist


def test_unconfigured_alerting_warns_instead_of_returning_silently():
    """The likelier misconfiguration used to be the silent one: a missing RESEND_API_KEY
    warned, a missing OPS_ALERT_EMAIL said nothing at all."""
    import inspect

    from src.workers import ops_alerts

    src = inspect.getsource(ops_alerts.send_ops_alert)
    head = src[:src.index("if not settings.RESEND_API_KEY")]
    assert "OPS_ALERT_EMAIL not configured" in head
    assert "_logger.warning" in head
    assert "silent no-op" not in src


def test_persist_records_a_system_row_and_never_raises():
    """Best-effort by contract: durability is additive and must never fail the caller
    (send_ops_alert is called from a Stripe webhook handler, among others)."""
    import inspect

    from src.workers import ops_alerts

    src = inspect.getsource(ops_alerts._persist_ops_alert)
    assert 'event="ops_alert"' in src
    assert "user_id=None" in src, "system-written row, not tenant-scoped"
    assert "except Exception" in src and "_logger.warning" in src
    # created_at client-side: a server_default triggers INSERT..RETURNING, which FORCE
    # RLS denies for the app role (same trap as _persist_audit_event).
    assert "created_at=datetime.now(UTC)" in src


def test_persisted_row_carries_the_alert_identity():
    """kind:key is what makes "this fired daily for four weeks" answerable later."""
    import inspect

    from src.workers import ops_alerts

    src = inspect.getsource(ops_alerts._persist_ops_alert)
    assert 'path=f"{kind}:{key}"[:256]' in src
    assert "{subject}" in src


def test_send_ops_alert_still_returns_false_when_undelivered():
    """The return value means DELIVERED, and callers may rely on that; recording an
    alert must not start reporting success."""
    import inspect

    from src.workers import ops_alerts

    src = inspect.getsource(ops_alerts.send_ops_alert)
    head = src[:src.index("if not _cooldown_acquired")]
    assert head.count("return False") >= 2
