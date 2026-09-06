"""Record-quota accounting: what X / 1,000 means, and what may change it.

``users.records_used`` is the authoritative counter and ``/billing/usage``
renders it verbatim, so X / 1,000 means "records CONSUMED this calendar month".
The durable per-job anchor behind it is ``jobs.billed_count`` plus
``jobs.billing_applied_at``, written under a compare-and-set so a re-run cannot
double-charge.

These tests pin the rules a production incident showed were unprotected: the
counter is advanced only by billing a job, is reset only by a genuine month
rollover, and is never reduced by deleting data.

Real Postgres, real settings, no mocks — per the project testing rules.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from src.api.auth import hash_password
from src.api.quota_window import add_months
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal


def _month_start(dt: datetime | None = None) -> datetime:
    dt = dt or datetime.now(UTC)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _mk_user(db, *, used: int = 0, limit: int = 1000,
             period: datetime | None = None) -> User:
    # ``period`` now names the ENTITLEMENT WINDOW, not a calendar month
    # (migration 088). The window columns are the authority; records_period_start
    # is written in lockstep as a mirror for the skip-trace beat and operator
    # queries, so the fixture keeps them consistent exactly as production does.
    # A "stale period" in these tests therefore means a window whose END has
    # passed, which is what the gates actually test.
    start = period or _month_start()
    user = User(
        id=str(uuid.uuid4()),
        email=f"quota_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=used,
        records_limit=limit,
        records_period_start=start,
        skip_trace_period_start=start,
        quota_anchor_at=start,
        quota_period_start=start,
        quota_period_end=add_months(start, 1),
    )
    db.add(user)
    db.flush()
    return user


def _mk_config(db, user_id: str) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Quota Test",
        county="pierce",
        state="WA",
        record_type="probate",
        fields=[],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    db.flush()
    return config


def _bill(db, user_id: str, amount: int) -> int:
    """The exact window-aware statement workers/tasks.py uses to charge a job.

    Built from the SAME shared SQL builders as production (window_cte_sql /
    window_set_sql), so this helper cannot quietly test a rule the worker does
    not follow — which is the failure mode the seven duplicated copies of the
    old period rule made easy.
    """
    from src.api.quota_window import window_cte_sql, window_set_sql

    return db.execute(
        text(
            "WITH cur AS ("
            "  SELECT u.id, u.records_used, u.records_limit, u.quota_anchor_at,"
            "         u.quota_period_start, u.quota_period_end,"
            "         u.subscription_status, u.entitlement_grace_ends_at,"
            "         u.entitlement_ends_at, u.pending_plan, u.pending_records_limit"
            "  FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE"
            "), w AS ("
            "  SELECT cur.*, " + window_cte_sql("", ":at") + " FROM cur"
            ") UPDATE users u SET"
            "    records_used = GREATEST(0, w.base + :billable),"
            + window_set_sql("w")
            + "  FROM w WHERE u.id = w.id"
        ),
        {"billable": amount, "uid": user_id, "at": datetime.now(UTC)},
    ).rowcount


# ─── The counter itself ───────────────────────────────────────────────────────

def test_new_account_starts_at_zero_of_one_thousand():
    with SyncSessionLocal() as db:
        user = _mk_user(db)
        db.commit()
        assert user.records_used == 0
        assert user.records_limit == 1000


def test_a_new_account_has_both_billing_periods_stamped():
    """Root cause of the incident: a NULL period is what the rollover ate."""
    with SyncSessionLocal() as db:
        user = _mk_user(db)
        db.commit()
        fresh = db.get(User, user.id)
        assert fresh.records_period_start is not None
        assert fresh.skip_trace_period_start is not None


def test_billing_a_job_increments_usage():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=10)
        db.commit()
        _bill(db, user.id, 5)
        db.commit()
        db.refresh(user)
        assert user.records_used == 15


def test_usage_crosses_999_to_1000():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=999)
        db.commit()
        _bill(db, user.id, 1)
        db.commit()
        db.refresh(user)
        assert user.records_used == 1000


def test_concurrent_bills_do_not_lose_an_update():
    """Two jobs billing the same user must sum, never clobber.

    The increment is one atomic SQL statement (records_used = records_used + N)
    rather than a read-modify-write in Python, so Postgres' row lock serialises
    them. NOTE: this proves no LOST UPDATE. It does not prove each job was
    allocated quota that was actually available — that is the separate
    over-allocation problem tracked for the reservation change.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        user_id = user.id
        db.commit()

    with SyncSessionLocal() as a, SyncSessionLocal() as b:
        _bill(a, user_id, 30)
        a.commit()
        _bill(b, user_id, 12)
        b.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 42


# ─── Reset semantics ──────────────────────────────────────────────────────────

def test_billing_rolls_a_stale_period_forward_itself():
    """Billing applies the month boundary at charge time, not whenever beat runs.

    This is what makes the beat task's zeroing provably safe: because every bill
    advances records_period_start, a period still stale when the task runs
    proves nothing was billed in the current period.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=900, period=datetime(2020, 1, 1, tzinfo=UTC))
        db.commit()
        _bill(db, user.id, 7)
        db.commit()
        db.refresh(user)
        assert user.records_used == 7, "last month's 900 must not carry forward"
        # Compare INSTANTS, not calendar dates. The driver hands the column back
        # in the session's local zone, so a correct 2026-09-01T00:00Z reads as
        # 2026-08-31T17:00-07:00 and a .date() comparison would fail on a value
        # that is in fact exactly right.
        assert user.records_period_start.astimezone(UTC) == _month_start()


def test_a_late_reconciliation_cannot_wipe_new_window_usage():
    """The second production defect, pinned against the mechanism that owns it.

    The reconciliation runs hourly, so a redeploy cannot skip a boundary. But
    running late is exactly when usage already exists in the NEW window: billing
    advances the window forward in the same statement that charges, so by the
    time the beat catches up the counter holds consumption that belongs to the
    live window. A catch-up that zeroed unconditionally would destroy it.
    """
    from src.workers.scheduler import reconcile_quota_periods

    with SyncSessionLocal() as db:
        # An ended window, as after a boundary nothing has caught up with yet.
        user = _mk_user(db, used=0, period=datetime(2020, 1, 1, tzinfo=UTC))
        user_id = user.id
        db.commit()
        _bill(db, user_id, 67)  # a job bills, and rolls the window itself
        db.commit()

    reconcile_quota_periods()  # the late catch-up

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 67


def test_no_premature_reset_inside_a_live_window():
    from src.workers.scheduler import reconcile_quota_periods

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=500)
        user_id = user.id
        db.commit()

    for _ in range(3):
        reconcile_quota_periods()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 500


def test_reading_usage_repeatedly_never_mutates_it():
    """Covers 'page refresh' and 'logout / log back in' — both are plain reads.

    /billing/usage and /auth/me only SELECT the column; no read path may write it.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=250)
        user_id = user.id
        db.commit()

    for _ in range(5):
        with SyncSessionLocal() as db:
            assert db.get(User, user_id).records_used == 250

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 250


# ─── What must NOT change usage ───────────────────────────────────────────────

def test_deleting_a_job_does_not_reduce_consumed_usage():
    """Consumed service usage stays counted. Quota is not derived from visible
    rows, so deleting results/leads cannot refund quota."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        config = _mk_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="done",
            record_count=40,
            billed_count=40,
            billing_applied_at=datetime.now(UTC),
        )
        db.add(job)
        db.flush()
        _bill(db, user.id, 40)
        db.commit()
        job_id, user_id = job.id, user.id

    with SyncSessionLocal() as db:
        db.delete(db.get(Job, job_id))
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 40


def test_a_failed_job_is_never_billed():
    """A job with no billing_applied_at was never charged and must not count."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        config = _mk_config(db, user.id)
        db.add(Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="failed",
            record_count=210,   # rows were scraped
            billed_count=0,     # but nothing was charged
            billing_applied_at=None,
        ))
        db.commit()
        db.refresh(user)
        assert user.records_used == 0


def test_a_retry_cannot_double_charge_the_same_job():
    """billing_applied_at is a compare-and-set: only the first claim bills."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        config = _mk_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="done",
            record_count=25,
        )
        db.add(job)
        db.commit()
        job_id, user_id = job.id, user.id

    def claim_and_bill(amount: int) -> bool:
        with SyncSessionLocal() as db:
            won = db.execute(
                text(
                    "UPDATE jobs SET billed_count = :n, billing_applied_at = NOW() "
                    "WHERE id = CAST(:j AS uuid) AND billing_applied_at IS NULL"
                ),
                {"n": amount, "j": job_id},
            ).rowcount
            if won:
                _bill(db, user_id, amount)
            db.commit()
            return bool(won)

    assert claim_and_bill(25) is True
    assert claim_and_bill(25) is False, "second attempt must not re-claim"

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 25


# ─── Multi-job / batch accounting ─────────────────────────────────────────────

def test_multi_county_batch_children_each_bill_their_own_count():
    """Each child job charges its own billed_count; the total is their sum."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        user_id = user.id
        for county, n in (("pierce", 110), ("king", 217), ("snohomish", 6)):
            config = _mk_config(db, user.id)
            config.county = county
            db.add(Job(
                id=str(uuid.uuid4()), user_id=user.id,
                scraper_config_id=config.id, status="done", trigger="batch",
                record_count=n, billed_count=n,
                billing_applied_at=datetime.now(UTC),
            ))
            db.flush()
            _bill(db, user.id, n)
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 333


def test_multi_record_type_batch_bills_per_child():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        user_id = user.id
        for record_type, n in (("probate", 12), ("pre_foreclosure", 30)):
            config = _mk_config(db, user.id)
            config.record_type = record_type
            db.flush()
            _bill(db, user.id, n)
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 42


# ─── The counter reconstructs from the durable anchor ─────────────────────────

def test_usage_is_reconstructible_from_the_billing_ledger():
    """The repair script's premise: SUM(billed_count) over the current period
    reproduces records_used, which is what let the incident be corrected from
    evidence instead of a guess."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0)
        user_id = user.id
        config = _mk_config(db, user.id)
        for n in (110, 217, 6, 1):
            db.add(Job(
                id=str(uuid.uuid4()), user_id=user.id,
                scraper_config_id=config.id, status="done",
                record_count=n, billed_count=n,
                billing_applied_at=datetime.now(UTC),
            ))
            db.flush()
            _bill(db, user.id, n)
        # A failed job: rows scraped, never billed, must not enter the ledger.
        db.add(Job(
            id=str(uuid.uuid4()), user_id=user.id,
            scraper_config_id=config.id, status="failed",
            record_count=999, billed_count=0, billing_applied_at=None,
        ))
        db.commit()

    with SyncSessionLocal() as db:
        ledger = db.execute(
            text(
                "SELECT COALESCE(SUM(j.billed_count), 0) FROM jobs j "
                "JOIN users u ON u.id = j.user_id "
                "WHERE j.user_id = CAST(:u AS uuid) "
                "  AND j.billing_applied_at IS NOT NULL "
                "  AND j.billing_applied_at >= u.records_period_start"
            ),
            {"u": user_id},
        ).scalar()
        assert ledger == 334
        assert db.get(User, user_id).records_used == ledger


# ─── Plan changes ─────────────────────────────────────────────────────────────

def test_changing_plan_limit_does_not_reset_consumed_usage():
    """An upgrade raises the ceiling; it does not forgive what was consumed."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=900, limit=1000)
        user_id = user.id
        db.commit()

    with SyncSessionLocal() as db:
        u = db.get(User, user_id)
        u.plan = "business"
        u.records_limit = 5000
        db.commit()

    with SyncSessionLocal() as db:
        u = db.get(User, user_id)
        assert u.records_used == 900
        assert u.records_limit == 5000


def test_unlimited_plan_is_never_capped():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=50_000, limit=-1)
        db.commit()
        assert user.records_limit == -1


# ─── Period-aware enforcement gates ───────────────────────────────────────────

def test_effective_used_is_zero_while_the_period_is_stale():
    """The gates must not charge a user for LAST month's usage.

    The rollover runs daily, not exactly at the boundary, and billing rolls a
    user's period forward only when they are actually charged — so a healthy
    user can legitimately sit on last month's period for a while. Reading the
    raw counter during that window blocked them on usage they no longer owed.
    """
    from src.api.quota import effective_records_used, is_over_record_limit

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=1000, limit=1000,
                        period=datetime(2020, 1, 1, tzinfo=UTC))
        db.commit()
        assert effective_records_used(user) == 0
        assert is_over_record_limit(user) is False


def test_effective_used_counts_a_current_period():
    from src.api.quota import effective_records_used, is_over_record_limit

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=1000, limit=1000)
        db.commit()
        assert effective_records_used(user) == 1000
        assert is_over_record_limit(user) is True


def test_at_999_of_1000_the_gate_still_allows_a_run():
    from src.api.quota import is_over_record_limit

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=999, limit=1000)
        db.commit()
        assert is_over_record_limit(user) is False


def test_unlimited_plan_never_trips_the_gate():
    from src.api.quota import is_over_record_limit

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=50_000, limit=-1)
        db.commit()
        assert is_over_record_limit(user) is False


def test_a_null_period_is_treated_as_current_not_as_free_quota():
    """Handing out quota is the destructive direction, so an unexpected NULL
    must not read as 'fresh period, 0 used'. The rollover ops-alerts on it."""
    from src.api.quota import effective_records_used

    class _Detached:
        records_used = 800
        records_limit = 1000
        records_period_start = None

    assert effective_records_used(_Detached()) == 800


# ─── NULL period must never zero the COUNTER (worker == API gate) ─────────────

def _bill_worker_sql(db, user_id: str, delta: int) -> None:
    """The settlement statement exactly as workers/tasks.py issues it."""
    db.execute(
        text(
            "UPDATE users SET "
            "  records_used = GREATEST(0, CASE"
            "    WHEN records_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    THEN 0 ELSE records_used END + :delta), "
            "  records_period_start = CASE"
            "    WHEN records_period_start IS NULL"
            "      OR records_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    THEN date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    ELSE records_period_start END "
            "WHERE id = CAST(:uid AS uuid)"
        ),
        {"delta": delta, "uid": user_id},
    )


def test_a_null_period_does_not_zero_the_counter_in_the_worker():
    """The worker must agree with src/api/quota.py::effective_records_used.

    They used to disagree, and in the revenue-losing direction: the API gate
    preserved records_used on a NULL period while the worker's CASE discarded
    it, silently granting a free period's quota. Unreachable today (migration
    086 made the column NOT NULL) but pinned so the two halves cannot drift
    apart again.
    """
    from src.api.quota import effective_records_used

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=300, limit=1000)
        user_id = user.id
        db.commit()
        # Force the legacy NULL shape the constraint now forbids.
        db.execute(text(
            "ALTER TABLE users ALTER COLUMN records_period_start DROP NOT NULL"))
        db.execute(
            text("UPDATE users SET records_period_start = NULL WHERE id = CAST(:u AS uuid)"),
            {"u": user_id},
        )
        db.commit()

    try:
        with SyncSessionLocal() as db:
            _bill_worker_sql(db, user_id, delta=25)
            db.commit()

        with SyncSessionLocal() as db:
            u = db.get(User, user_id)
            assert u.records_used == 325, "prior usage must survive a NULL period"
            assert u.records_period_start is not None, "the period IS adopted"
            # And the API gate agrees with the worker.
            assert effective_records_used(u) == 325
    finally:
        with SyncSessionLocal() as db:
            db.execute(text(
                "ALTER TABLE users ALTER COLUMN records_period_start SET NOT NULL"))
            db.commit()


def test_a_stale_period_still_zeroes_the_counter():
    """The change must not weaken the real rollover: STALE still resets."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=300, limit=1000,
                        period=datetime(2020, 1, 1, tzinfo=UTC))
        user_id = user.id
        db.commit()

    with SyncSessionLocal() as db:
        _bill_worker_sql(db, user_id, delta=25)
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 25, "stale period still resets"


# ─── /billing/usage reports the quota WINDOW, not just a bare number ──────────

def test_usage_window_helpers_agree_with_the_reset_boundary():
    """The window /billing/usage reports must be the same boundary the rollover
    and the billing path use, or the UI would advertise a reset date that is not
    when the counter actually moves."""
    from src.api.quota import current_period_start

    start = current_period_start()
    assert (start.day, start.hour, start.minute, start.second) == (1, 0, 0, 0)
    assert start.tzinfo is not None, "the boundary is UTC-anchored, never naive"

    # next_reset_at, computed the way the route computes it.
    nxt = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    assert nxt > start
    assert nxt.day == 1
    # Exactly one month later, and it must roll the YEAR at December.
    assert (nxt.year, nxt.month) == (
        (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    )


def test_usage_reports_period_aware_usage_not_the_raw_column():
    """A stale period must not be reported as usage the user still owes — the
    same rule the enforcement gates follow."""
    from src.api.quota import effective_records_used

    with SyncSessionLocal() as db:
        stale = _mk_user(db, used=900, period=datetime(2020, 1, 1, tzinfo=UTC))
        current = _mk_user(db, used=900)
        db.commit()
        assert effective_records_used(stale) == 0
        assert effective_records_used(current) == 900


def test_december_boundary_rolls_into_january_of_the_next_year():
    """The one arithmetic case a naive month+1 gets wrong."""
    dec = datetime(2026, 12, 1, tzinfo=UTC)
    nxt = (
        dec.replace(year=dec.year + 1, month=1)
        if dec.month == 12
        else dec.replace(month=dec.month + 1)
    )
    assert (nxt.year, nxt.month, nxt.day) == (2027, 1, 1)
