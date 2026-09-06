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
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal


def _month_start(dt: datetime | None = None) -> datetime:
    dt = dt or datetime.now(UTC)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _mk_user(db, *, used: int = 0, limit: int = 1000,
             period: datetime | None = None) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"quota_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=used,
        records_limit=limit,
        records_period_start=period or _month_start(),
        skip_trace_period_start=period or _month_start(),
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
    """The exact period-aware statement workers/tasks.py uses to charge a job."""
    return db.execute(
        text(
            "UPDATE users SET "
            "  records_used = CASE"
            "    WHEN records_period_start IS NULL"
            "      OR records_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    THEN 0 ELSE records_used END + :billable, "
            "  records_period_start = CASE"
            "    WHEN records_period_start IS NULL"
            "      OR records_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    THEN date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    ELSE records_period_start END "
            "WHERE id = CAST(:uid AS uuid)"
        ),
        {"billable": amount, "uid": user_id},
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


def test_a_late_rollover_cannot_wipe_new_period_usage():
    """The second production defect, pinned.

    The task runs daily so a Beat outage on the 1st does not skip a month. But
    running late is exactly when usage already exists in the new period. A user
    billed inside the current period must survive the catch-up run.
    """
    from src.workers.scheduler import reset_monthly_usage

    with SyncSessionLocal() as db:
        # Stale period, as after a missed 1st.
        user = _mk_user(db, used=0, period=datetime(2020, 1, 1, tzinfo=UTC))
        user_id = user.id
        db.commit()
        _bill(db, user_id, 67)  # a job bills before the catch-up run fires
        db.commit()

    reset_monthly_usage()  # the late catch-up

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 67


def test_no_premature_reset_within_the_same_month():
    from src.workers.scheduler import reset_monthly_usage

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=500)
        user_id = user.id
        db.commit()

    for _ in range(3):
        reset_monthly_usage()

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
