"""Account deletion beat (P3b): Stripe cancel/un-cancel, the scheduled notice, alerts,
the skip-trace gate, and the purge driver end to end (R2, data, tombstone, completion,
Stripe Customer).

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
    def __init__(self, subs: dict[str, tuple[str, bool]] | None = None, fail: bool = False,
                 customers: dict[str, str] | None = None):
        self.subs = dict(subs or {})
        self.fail = fail
        self.customers = dict(customers or {})  # id -> 'open' | 'closable'
        self.calls: list[tuple] = []

    def customer_state(self, customer_id):
        return self.customers.get(customer_id, "missing")

    def delete_customer(self, customer_id, key):
        self.calls.append(("delete_customer", customer_id, key))
        self.customers.pop(customer_id, None)

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


class FakeR2:
    """In-memory bucket. Lists at most 2 keys per call, so the sweep's paging is real."""

    def __init__(self, keys=(), fail=()):
        self.keys = set(keys)
        self.fail = set(fail)

    def list(self, prefix):
        return sorted(k for k in self.keys if k.startswith(prefix))[:2]

    def delete(self, key):
        if key in self.fail:
            return False
        self.keys.discard(key)
        return True


def _run(stripe=None, send=None, r2=None, send_final=None):
    return beat._drive_account_deletions_impl(
        stripe_api=stripe or FakeStripe(), send=send or (lambda to, when: None),
        r2=r2 or FakeR2(), send_final=send_final or (lambda to: None))


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


def test_the_notice_names_the_date_and_how_to_restore() -> None:
    subject, preheader, lines, cta = beat._scheduled_notice(
        datetime(2026, 11, 5, 17, tzinfo=UTC))
    assert "scheduled for deletion" in subject
    assert "November 05, 2026" in preheader and "November 05, 2026" in lines[0]
    assert any("not renew" in line for line in lines)
    assert cta == ("Sign in to restore", f"{settings.FRONTEND_URL}/login")


def test_no_notice_before_billing_is_confirmed_stopped(made, resend_on) -> None:
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _, did, _ = _account(made, sub=sub)
    sent: list = []
    _run(FakeStripe(fail=True), send=lambda to, when: sent.append(to))
    assert sent == [] and _row(did).stripe_state == "pending_cancel"


# ── Alerts, single run ───────────────────────────────────────────────────────────

def test_a_deletion_overdue_by_ten_days_alerts_ops(made) -> None:
    sub = f"sub_{uuid.uuid4().hex[:12]}"
    _, did, _ = _account(made, sub=sub, overdue=True)
    assert _run(FakeStripe(fail=True))["overdue_alerts"] >= 1  # billing never confirmed
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


# ── The purge driver (P3b-2) ─────────────────────────────────────────────────────

def _as_purge(sql: str, params: dict) -> None:
    with sync_engine.begin() as c:
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text(sql), params)


def _seeded(made, **kw):
    """A due account holding a row in every matrix table, plus its R2 objects."""
    from tests.test_account_deletion_purge import _seed

    uid, did, email = _account(made, overdue=True, **kw)
    with sync_engine.begin() as c:
        ids = _seed(c, uid)
        c.execute(text("UPDATE jobs SET export_key = :k WHERE id = :j"),
                  {"k": f"exports/{uid}/{ids['job']}/leads.csv", "j": ids["job"]})
        pend = c.execute(text(
            "SELECT user_id, property_address, city, state, trace_type, first_name, "
            "last_name FROM pending_skip_trace_rows WHERE user_id = :u"), {"u": uid}).one()
        from src.scrapers.enrichment.skip_trace import pending_row_subject_key
        ids["cachekey"] = pending_row_subject_key(pend)
        c.execute(text("INSERT INTO skip_trace_cache (address_hash, phone) VALUES (:k, '555')"),
                  {"k": ids["cachekey"]})
    r2 = FakeR2({f"exports/{uid}/{ids['job']}/leads.csv", f"exports/{uid}/batch/x/combined.csv",
                 f"exports/{uid}/a.csv", "exports/someone-else/keep.csv"})
    return uid, did, email, ids, r2


def _user_row(uid: str):
    with sync_engine.connect() as c:
        return c.execute(text("SELECT * FROM users WHERE id = :u"), {"u": uid}).one()


def _cleanup_cache(ids) -> None:
    """Rows _seed writes with no foreign key to users (deleting the user leaves them)."""
    with sync_engine.begin() as c:
        c.execute(text("DELETE FROM skip_trace_cache WHERE address_hash IN (:a, :b)"),
                  {"a": ids["subject"], "b": ids["cachekey"]})
        c.execute(text("DELETE FROM pending_registrations WHERE email_hmac = :h"),
                  {"h": ids["hmac"]})


def _due_now(did: str) -> None:
    _as_purge("UPDATE account_deletions SET next_attempt_at = now() - interval '1 second', "
              "claimed_until = LEAST(claimed_until, now() - interval '1 second') "
              "WHERE id = :d", {"d": did})


def _a_day_later(did: str) -> None:
    _as_purge("UPDATE account_deletions SET r2_first_sweep_at = r2_first_sweep_at "
              "- interval '25 hours', next_attempt_at = now() - interval '1 second' "
              "WHERE id = :d", {"d": did})


def test_the_whole_deletion_from_claim_to_completion(made) -> None:
    from src.utils.crypto import decrypt_field

    uid, did, email, ids, r2 = _seeded(made)
    try:
        finals: list = []
        result = _run(r2=r2, send_final=finals.append)
        assert result["purges"] == {"tombstoned": 1}
        # R2: the account's prefix and recorded keys gone, other tenants untouched.
        assert r2.keys == {"exports/someone-else/keep.csv"}
        # The final email went to the ORIGINAL address, before the tombstone.
        assert finals == [email]
        user = _user_row(uid)
        placeholder = f"deleted+{uid}@invalid"
        assert decrypt_field(user.email) == placeholder
        assert user.email_hmac == blind_index(placeholder)
        assert (user.is_active, user.deletion_state, user.first_name) == (False, "purging", None)
        with sync_engine.connect() as c:
            # The original address is free to register again.
            assert c.execute(text("SELECT count(*) FROM users WHERE email_hmac = :h"),
                             {"h": blind_index(email)}).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM results WHERE user_id = :u "
                                  "AND party_name IS NOT NULL"), {"u": uid}).scalar() == 0
            assert c.execute(text("SELECT count(*) FROM skip_trace_cache "
                                  "WHERE address_hash = :k"), {"k": ids["cachekey"]}).scalar() == 0
        row = _row(did)
        assert row.status == "purging" and row.tombstoned_at is not None
        assert _run(r2=r2)["purges"] == {}  # parked: nothing before 24 h

        # 24 h later (moved by hand): the second pass completes, and with no Stripe
        # customer the billing side is done too.
        _a_day_later(did)
        result = _run(r2=r2)
        assert result["purges"] == {"completed": 1} and result["customers_deleted"] == 1
        row = _row(did)
        assert (row.status, row.stripe_state) == ("completed", "customer_deleted")
        assert _user_row(uid).deletion_state == "deleted"
    finally:
        _cleanup_cache(ids)


def test_work_in_flight_defers_the_purge_until_day_40(made) -> None:
    from tests.test_account_deletion_purge import _seed

    uid, did, _ = _account(made, stripe_state="not_applicable")
    with sync_engine.begin() as c:
        ids = _seed(c, uid)
        c.execute(text("UPDATE jobs SET status = 'running' WHERE id = :j"), {"j": ids["job"]})
        # purge_after is immutable: replace the row with one that fell due yesterday.
        c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        c.execute(text("UPDATE account_deletions SET status = 'restored' WHERE id = :d"),
                  {"d": did})
        did = str(c.execute(text(
            "INSERT INTO account_deletions (user_id, status, purge_after, stripe_state) "
            "VALUES (:u, 'pending', now() - interval '1 day', 'not_applicable') "
            "RETURNING id"), {"u": uid}).scalar())
    try:
        assert _run()["purges"] == {}
        row = _row(did)
        assert (row.status, row.last_error) == ("pending", "waiting for in-flight work")
        assert row.next_attempt_at > datetime.now(UTC)
        with sync_engine.begin() as c:
            c.execute(text("UPDATE jobs SET status = 'done' WHERE id = :j"), {"j": ids["job"]})
            c.execute(text("UPDATE batch_runs SET status = 'completed' WHERE user_id = :u"),
                      {"u": uid})
        _due_now(did)
        assert _run()["purges"] == {"tombstoned": 1}
    finally:
        _cleanup_cache(ids)

    # Past day 40 the CCPA deadline wins: purged under the write fence anyway.
    uid2, did2, _, ids2, r2 = _seeded(made)
    try:
        with sync_engine.begin() as c:
            c.execute(text("UPDATE jobs SET status = 'running' WHERE id = :j"), {"j": ids2["job"]})
        assert _run(r2=r2)["purges"] == {"tombstoned": 1}
    finally:
        _cleanup_cache(ids2)


def test_an_r2_failure_fails_closed_and_retries(made) -> None:
    uid, did, _, ids, r2 = _seeded(made)
    try:
        r2.fail = {f"exports/{uid}/a.csv"}
        assert _run(r2=r2)["purges"] == {"error": 1}
        row = _row(did)
        assert row.status == "purging" and row.r2_first_sweep_at is None
        assert "RuntimeError" in row.last_error
        r2.fail = set()
        _due_now(did)
        assert _run(r2=r2)["purges"] == {"tombstoned": 1}
    finally:
        _cleanup_cache(ids)


def test_a_crash_after_the_data_purge_resumes_at_the_email(made) -> None:
    uid, did, email, ids, r2 = _seeded(made)
    try:
        def broken(to):
            raise ConnectionError("resend down")

        assert _run(r2=r2, send_final=broken)["purges"] == {"error": 1}
        row = _row(did)
        assert row.db_purged_at is not None and row.final_email_sent_at is None
        assert _user_row(uid).email_hmac == blind_index(email)  # not tombstoned yet
        _due_now(did)
        finals: list = []
        assert _run(r2=r2, send_final=finals.append)["purges"] == {"tombstoned": 1}
        assert finals == [email]
    finally:
        _cleanup_cache(ids)


def test_out_of_time_pauses_and_the_next_claim_resumes(made, monkeypatch) -> None:
    uid, did, _, ids, r2 = _seeded(made)
    try:
        monkeypatch.setattr(beat, "_LEASE_MARGIN", beat._LEASE_SECONDS)  # no time left
        assert _run(r2=r2)["purges"] == {"paused": 1}
        row = _row(did)
        assert row.r2_first_sweep_at is None and row.last_error == "paused: out of time"
        assert row.claimed_until <= datetime.now(UTC)  # the lease was handed back
        monkeypatch.setattr(beat, "_LEASE_MARGIN", 120)
        _due_now(did)  # the lease ran out
        assert _run(r2=r2)["purges"] == {"tombstoned": 1}
    finally:
        _cleanup_cache(ids)


def test_the_stripe_customer_goes_only_once_nothing_is_open(made) -> None:
    cus = f"cus_{uuid.uuid4().hex[:12]}"
    uid, did, _, ids, r2 = _seeded(made)
    try:
        with sync_engine.begin() as c:
            c.execute(text("UPDATE users SET stripe_customer_id = :c WHERE id = :u"),
                      {"c": cus, "u": uid})
        stripe = FakeStripe(customers={cus: "open"})
        _run(stripe, r2=r2)
        _a_day_later(did)
        result = _run(stripe, r2=r2)
        assert result["purges"] == {"completed": 1} and result["customers_deleted"] == 0
        assert _row(did).stripe_state == "not_applicable" and stripe.calls == []
        stripe.customers[cus] = "closable"
        _due_now(did)
        assert _run(stripe, r2=r2)["customers_deleted"] == 1
        assert stripe.calls == [("delete_customer", cus, f"acctdel-{did}-customer")]
        assert _row(did).stripe_state == "customer_deleted"
    finally:
        _cleanup_cache(ids)


def test_a_stale_claim_cannot_commit_the_tombstone(made) -> None:
    from src.db.session import system_sync_session

    uid, did, email = _account(made)
    with system_sync_session() as db, pytest.raises(beat._OutOfTimeError):
        beat._tombstone(db, uid, did, uuid.uuid4())  # not the live claim
    assert _user_row(uid).email_hmac == blind_index(email)


def test_a_stripe_customer_left_past_any_billing_period_alerts_ops(made) -> None:
    cus = f"cus_{uuid.uuid4().hex[:12]}"
    uid, did, _, ids, r2 = _seeded(made)
    try:
        with sync_engine.begin() as c:
            c.execute(text("UPDATE users SET stripe_customer_id = :c WHERE id = :u"),
                      {"c": cus, "u": uid})
        stripe = FakeStripe(customers={cus: "open"})
        _run(stripe, r2=r2)
        _a_day_later(did)
        assert _run(stripe, r2=r2)["stuck_alerts"] == 0  # waiting is normal
        _as_purge("UPDATE account_deletions SET completed_at = now() - interval '401 days' "
                  "WHERE id = :d", {"d": did})
        assert _run(stripe, r2=r2)["stuck_alerts"] >= 1
    finally:
        _cleanup_cache(ids)


def test_completion_waits_for_a_linked_batch_then_retries(made) -> None:
    import random

    uid, did, _, ids, r2 = _seeded(made)
    n = random.randint(1, 2**31 - 1)
    try:
        assert _run(r2=r2)["purges"] == {"tombstoned": 1}
        with sync_engine.begin() as c:  # a Tracerfy batch of the account still in flight
            c.execute(text("INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id) "
                           "VALUES (gen_random_uuid(), :n, :u)"), {"n": n, "u": uid})
        _a_day_later(did)
        assert _run(r2=r2)["purges"] == {"error": 1}
        row = _row(did)
        assert row.status == "purging" and "BLD36" in row.last_error
        assert row.r2_final_sweep_at is not None
        with sync_engine.begin() as c:
            c.execute(text("UPDATE skip_trace_queues SET status = 'completed' "
                           "WHERE tracerfy_queue_id = :n"), {"n": n})
        _due_now(did)
        assert _run(r2=r2)["purges"] == {"completed": 1}
        assert _row(did).r2_final_sweep_at == row.r2_final_sweep_at  # first marker kept
    finally:
        with sync_engine.begin() as c:
            c.execute(text("DELETE FROM skip_trace_queues WHERE tracerfy_queue_id = :n"),
                      {"n": n})
        _cleanup_cache(ids)


def test_no_account_with_work_in_flight_is_claimed_past_one_batch(made, monkeypatch) -> None:
    """More in-flight accounts than one deferral batch: every one is deferred before
    any claim, so none is purged while its work still runs."""
    from tests.test_account_deletion_purge import _seed

    monkeypatch.setattr(beat, "_BATCH", 2)
    cleanup, dids = [], []
    for _ in range(5):
        uid, did, _ = _account(made, stripe_state="not_applicable")
        with sync_engine.begin() as c:
            ids = _seed(c, uid)
            cleanup.append(ids)
            c.execute(text("UPDATE jobs SET status = 'running' WHERE id = :j"), {"j": ids["job"]})
            c.execute(text("SET LOCAL ROLE bridgeleads_purge"))
            c.execute(text("UPDATE account_deletions SET status = 'restored' WHERE id = :d"),
                      {"d": did})
            dids.append(str(c.execute(text(
                "INSERT INTO account_deletions (user_id, status, purge_after, stripe_state) "
                "VALUES (:u, 'pending', now() - interval '1 day', 'not_applicable') "
                "RETURNING id"), {"u": uid}).scalar()))
    try:
        result = _run()
        assert result["purges"] == {} and result["deferred"] == 5
        assert {_row(d).status for d in dids} == {"pending"}
    finally:
        for ids in cleanup:
            _cleanup_cache(ids)
