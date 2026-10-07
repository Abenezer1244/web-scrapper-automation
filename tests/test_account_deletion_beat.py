"""Account deletion beat, P3b-1: Stripe cancel/un-cancel, the scheduled notice, overdue
alerts, and the skip-trace gate for accounts being deleted.

Real database: the task opens its own sessions, so every test commits its rows and
deletes its users afterwards (users CASCADE to their deletion rows). Stripe and Resend
are external, rate-limited APIs: the task takes a Stripe stand-in and a send callable
(precedent: _expire_trials_impl(subscription_lookup=)), which record what was asked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.api.auth import hash_password
from src.config import settings
from src.db.session import sync_engine
from src.utils.crypto import blind_index, encrypt_field
from src.workers import account_deletion_beat as beat
from src.workers.skip_trace_claim import ACCESS_ENDED, paid_lookup_access, read_access_rows


class FakeStripe:
    def __init__(self, subs: dict[str, tuple[str, bool]] | None = None, fail: bool = False):
        self.subs = dict(subs or {})
        self.fail = fail
        self.calls: list[tuple] = []

    def status(self, subscription_id):
        if self.fail:
            raise ConnectionError("stripe down")
        return self.subs.get(subscription_id)

    def set_cancel_at_period_end(self, subscription_id, value, key):
        self.calls.append((subscription_id, value, key))
        status, _ = self.subs[subscription_id]
        self.subs[subscription_id] = (status, value)


@pytest.fixture
def made():
    """Users created by a test; deleted (with their deletion rows) afterwards."""
    users: list[str] = []
    yield users
    with sync_engine.begin() as c:
        for uid in users:
            c.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})


@pytest.fixture
def resend_on(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")


def _account(made, *, sub: str | None = None, status: str = "pending",
             stripe_state: str = "pending_cancel", overdue: bool = False) -> tuple[str, str, str]:
    """(user_id, deletion_id, email) for an account in the given deletion state."""
    uid = str(uuid.uuid4())
    email = f"beat_{uid[:8]}@bl.test"
    with sync_engine.begin() as c:
        c.execute(text(
            "INSERT INTO users (id, email, email_hmac, password_hash, stripe_subscription_id) "
            "VALUES (:u, :e, :h, :pw, :s)"),
            {"u": uid, "e": encrypt_field(email), "h": blind_index(email),
             "pw": hash_password("Pw-123456789"), "s": sub})
        made.append(uid)
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text("UPDATE users SET deletion_state = 'pending' WHERE id = :u"), {"u": uid})
        did = c.execute(text(
            "INSERT INTO account_deletions (user_id, status, purge_after, stripe_state) "
            "VALUES (:u, 'pending', CASE WHEN :o THEN timestamptz '1990-01-01' "
            "ELSE now() + interval '30 days' END, :s) RETURNING id"),
            {"u": uid, "o": overdue, "s": stripe_state}).scalar()
        if status == "restored":
            c.execute(text("UPDATE account_deletions SET status = 'restored', "
                           "stripe_state = 'pending_uncancel' WHERE id = :d"), {"d": did})
            c.execute(text("UPDATE users SET deletion_state = NULL WHERE id = :u"), {"u": uid})
    return uid, str(did), email


def _row(did: str):
    with sync_engine.connect() as c:
        return c.execute(text("SELECT * FROM account_deletions WHERE id = :d"),
                         {"d": did}).one()


def _run(stripe=None, send=None):
    return beat._drive_account_deletions_impl(
        stripe_api=stripe or FakeStripe(), send=send or (lambda to, when: None))


# ── Stripe ───────────────────────────────────────────────────────────────────────

def test_a_pending_deletion_stops_the_renewal(made) -> None:
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _, did, _ = _account(made, sub=sub)
    stripe = FakeStripe({sub: ("active", False)})
    _run(stripe)
    assert stripe.calls == [(sub, True, f"acctdel-{did}-cancel")]
    assert _row(did).stripe_state == "cancel_set"
    _run(stripe)  # done: nothing asked again
    assert len(stripe.calls) == 1


@pytest.mark.parametrize("sub_state", [None, ("canceled", False), ("active", True), "missing"])
def test_nothing_to_cancel_still_confirms_billing_is_stopped(made, sub_state) -> None:
    sub = None if sub_state is None else f"sub_{uuid.uuid4().hex[:12]}"
    _, did, _ = _account(made, sub=sub)
    stripe = FakeStripe({} if sub_state in (None, "missing") else {sub: sub_state})
    _run(stripe)
    assert stripe.calls == []
    expected = "cancel_set" if sub_state == ("active", True) else "not_applicable"
    assert _row(did).stripe_state == expected


def test_a_restore_takes_the_cancellation_back(made) -> None:
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _, did, _ = _account(made, sub=sub, status="restored")
    stripe = FakeStripe({sub: ("active", True)})
    _run(stripe)
    assert stripe.calls == [(sub, False, f"acctdel-{did}-uncancel")]
    assert _row(did).stripe_state == "uncancel_set"


def test_a_stripe_failure_backs_off_and_retries(made) -> None:
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _, did, _ = _account(made, sub=sub)
    _run(FakeStripe(fail=True))
    row = _row(did)
    assert (row.stripe_state, row.attempts) == ("pending_cancel", 1)
    assert "stripe pending_cancel: ConnectionError" in row.last_error
    assert row.next_attempt_at > datetime.now(UTC)
    healthy = FakeStripe({sub: ("active", False)})
    _run(healthy)  # still inside the backoff
    assert healthy.calls == []
    with sync_engine.begin() as c:
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text("UPDATE account_deletions SET next_attempt_at = now() WHERE id = :d"),
                  {"d": did})
    _run(healthy)
    assert _row(did).stripe_state == "cancel_set"


# ── Scheduled notice ─────────────────────────────────────────────────────────────

def test_the_scheduled_notice_goes_once_to_the_account_address(made, resend_on) -> None:
    _, did, email = _account(made)
    sent: list = []
    _run(send=lambda to, when: sent.append((to, when)))
    assert len(sent) == 1 and sent[0][0] == email
    assert sent[0][1] == _row(did).purge_after
    assert _row(did).scheduled_email_sent_at is not None
    _run(send=lambda to, when: sent.append((to, when)))
    assert len(sent) == 1


def test_a_failed_notice_is_retried_and_no_key_sends_nothing(made, resend_on, monkeypatch) -> None:
    _, did, _ = _account(made)

    def broken(to, when):
        raise ConnectionError("resend down")

    _run(send=broken)
    row = _row(did)
    assert row.scheduled_email_sent_at is None and row.attempts == 1
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    sent: list = []
    with sync_engine.begin() as c:
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text("UPDATE account_deletions SET next_attempt_at = now() WHERE id = :d"),
                  {"d": did})
    _run(send=lambda to, when: sent.append(to))
    assert sent == [] and _row(did).scheduled_email_sent_at is None


def test_the_real_notice_renders(monkeypatch) -> None:
    """The default sender builds a real Resend payload (Resend itself not called)."""
    import resend

    captured: list = []
    monkeypatch.setattr(resend.Emails, "send", lambda payload: captured.append(payload))
    beat._send_scheduled_notice("x@bl.test", datetime(2026, 11, 5, 17, tzinfo=UTC))
    (payload,) = captured
    assert payload["to"] == ["x@bl.test"]
    assert "November 05, 2026" in payload["text"] and "/login" in payload["html"]


# ── Alerts, single run ───────────────────────────────────────────────────────────

def test_a_deletion_overdue_by_ten_days_alerts_ops(made) -> None:
    _, did, _ = _account(made, overdue=True)
    assert _run()["overdue_alerts"] >= 1
    with sync_engine.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM audit_events WHERE event = 'ops_alert' "
            "AND detail LIKE '%Account deletion overdue%' AND created_at > now() - "
            "interval '1 minute'")).scalar() >= 1


def test_only_one_run_at_a_time(made) -> None:
    with sync_engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": beat._LOCK_KEY})
        try:
            assert _run() == {"skipped": True}
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": beat._LOCK_KEY})


# ── Skip-trace gate ──────────────────────────────────────────────────────────────

def test_an_account_being_deleted_buys_no_lookups(made) -> None:
    uid, _, _ = _account(made)
    with sync_engine.connect() as c:
        row = read_access_rows(c, [uid], lock="")[uid]
    assert row.deletion_state == "pending"
    assert paid_lookup_access(row, datetime.now(UTC) + timedelta(seconds=1)) == ACCESS_ENDED
