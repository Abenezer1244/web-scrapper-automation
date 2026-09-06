"""Quota RESERVATION: concurrent jobs cannot be allocated the same allowance.

The plan cap used to READ remaining quota, cap the delivery to it, and only
charge ``users.records_used`` much later — in a different transaction, after the
export had been written. Nothing held the quota in between, so two jobs running
for one user (or two children of one multi-county batch, which fan out exactly
that way) could both read "100 remaining" and both deliver 100.

The atomic ``records_used = records_used + N`` increment prevents a LOST UPDATE,
so the totals sum correctly — but summing correctly is not allocating correctly.
It recorded the over-delivery faithfully instead of preventing it.

A lock cannot span that gap because the cap block commits before the export
runs. So the grant is RESERVED in the same atomic statement that computes it
(migration 087) and billing later settles only the delta.

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
        email=f"resv_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
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
        name="Reservation Test",
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


def _mk_job(db, user_id: str, config_id: str) -> Job:
    job = Job(
        id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=config_id,
        status="enriching",
    )
    db.add(job)
    db.flush()
    return job


_RESERVE_SQL = (
    "WITH grant_calc AS ("
    "  SELECT LEAST(:want, GREATEST(0, u.records_limit - CASE"
    "      WHEN u.records_period_start IS NULL"
    "        OR u.records_period_start < date_trunc('month', CAST(:at AS timestamptz) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    "      THEN 0 ELSE u.records_used END)) AS granted"
    "  FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE"
    "), claim AS ("
    "  UPDATE jobs SET reserved_count = (SELECT granted FROM grant_calc),"
    "                  reserved_at = CAST(:at AS timestamptz)"
    "  WHERE id = :jid AND reserved_at IS NULL"
    "  RETURNING reserved_count"
    "), charge AS ("
    "  UPDATE users SET"
    "    records_used = CASE"
    "      WHEN records_period_start IS NULL"
    "        OR records_period_start < date_trunc('month', CAST(:at AS timestamptz) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    "      THEN 0 ELSE records_used END"
    "      + COALESCE((SELECT reserved_count FROM claim), 0),"
    "    records_period_start = CASE"
    "      WHEN records_period_start IS NULL"
    "        OR records_period_start < date_trunc('month', CAST(:at AS timestamptz) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    "      THEN date_trunc('month', CAST(:at AS timestamptz) AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
    "      ELSE records_period_start END"
    "  WHERE id = CAST(:uid AS uuid)"
    ") SELECT COALESCE((SELECT reserved_count FROM claim), -1)"
)


def _reserve(db, job_id: str, user_id: str, want: int) -> int:
    """The atomic reserve-and-charge the plan cap performs (workers/tasks.py).

    Returns the granted amount, or -1 when this job had already reserved.
    """
    at = db.execute(text("SELECT clock_timestamp()")).scalar()
    return db.execute(
        text(_RESERVE_SQL),
        {"want": want, "uid": user_id, "jid": job_id, "at": at},
    ).scalar()


def _settle(db, user_id: str, delta: int) -> None:
    """The delta settlement the billing block applies."""
    db.execute(
        text(
            "UPDATE users SET records_used = GREATEST(0, CASE"
            "    WHEN records_period_start IS NULL"
            "      OR records_period_start < date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            "    THEN 0 ELSE records_used END + :delta) "
            "WHERE id = CAST(:uid AS uuid)"
        ),
        {"delta": delta, "uid": user_id},
    )


# ─── The over-allocation bug itself ───────────────────────────────────────────

def test_two_concurrent_jobs_cannot_both_take_the_same_remaining_quota():
    """Both jobs want 80 and only 100 remain.

    Read-then-cap let each see "100 remaining" and deliver 80, handing the user
    160 records on a 100-record allowance. Reserving makes the second job see
    what the first already took.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=900, limit=1000)      # 100 remaining
        config = _mk_config(db, user.id)
        job_a = _mk_job(db, user.id, config.id)
        job_b = _mk_job(db, user.id, config.id)
        user_id, a_id, b_id = user.id, job_a.id, job_b.id
        db.commit()

    with SyncSessionLocal() as sess_a, SyncSessionLocal() as sess_b:
        granted_a = _reserve(sess_a, a_id, user_id, want=80)
        sess_a.commit()
        granted_b = _reserve(sess_b, b_id, user_id, want=80)
        sess_b.commit()

    assert granted_a == 80, "first job takes what it asked for"
    assert granted_b == 20, "second job gets only what is genuinely left"

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 1000, "never past the cap"


def test_a_multi_county_batch_fanout_cannot_exceed_the_plan():
    """Five children each wanting 300 against a 1,000 cap must total 1,000."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job_ids = [_mk_job(db, user.id, config.id).id for _ in range(5)]
        user_id = user.id
        db.commit()

    grants = []
    for jid in job_ids:
        with SyncSessionLocal() as db:
            grants.append(_reserve(db, jid, user_id, want=300))
            db.commit()

    assert grants == [300, 300, 300, 100, 0], grants
    assert sum(grants) == 1000

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 1000


def test_reservation_is_clamped_to_zero_when_already_at_cap():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=1000, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        assert _reserve(db, job_id, user_id, want=50) == 0
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 1000


def test_a_rerun_reuses_its_grant_instead_of_reserving_twice():
    """reserved_at is a compare-and-set: a watchdog re-run must not take a
    second grant on top of the one it already holds."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        assert _reserve(db, job_id, user_id, want=30) == 30
        db.commit()
    with SyncSessionLocal() as db:
        assert _reserve(db, job_id, user_id, want=30) == -1, "already reserved"
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 30, "charged once, not twice"
        assert db.get(Job, job_id).reserved_count == 30


def test_reservation_rolls_a_stale_period_forward_like_billing_does():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=900, limit=1000,
                        period=datetime(2020, 1, 1, tzinfo=UTC))
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        # Stale period => 0 effective used => the full 400 is available.
        assert _reserve(db, job_id, user_id, want=400) == 400
        db.commit()

    with SyncSessionLocal() as db:
        user = db.get(User, user_id)
        assert user.records_used == 400, "last month's 900 must not carry forward"
        assert user.records_period_start.astimezone(UTC) == _month_start()


# ─── Release ──────────────────────────────────────────────────────────────────

def test_releasing_a_reservation_hands_the_quota_back():
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=40)
        db.commit()
        db.expire_all()  # reserve is raw SQL; drop the stale identity-map copy
        assert db.get(User, user_id).records_used == 40

    with SyncSessionLocal() as db:
        assert release_quota_reservation(db, job_id) == 40

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 0
        refreshed = db.get(Job, job_id)
        assert refreshed.reserved_count == 0
        assert refreshed.reserved_at is None, "cleared so a re-run can reserve"


def test_releasing_twice_is_a_no_op():
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=25)
        db.commit()

    with SyncSessionLocal() as db:
        assert release_quota_reservation(db, job_id) == 25
    with SyncSessionLocal() as db:
        assert release_quota_reservation(db, job_id) == 0, "frees nothing twice"

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 0, "not refunded twice"


def test_a_billed_job_is_never_refunded_by_a_release():
    """A job that settled its own delta owns its charge."""
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=60)
        db.commit()
        db.execute(
            text("UPDATE jobs SET billing_applied_at = NOW(), billed_count = 60 "
                 "WHERE id = :j"),
            {"j": job_id},
        )
        db.commit()

    with SyncSessionLocal() as db:
        assert release_quota_reservation(db, job_id) == 0

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 60


def test_a_release_after_the_period_rolled_does_not_eat_new_usage():
    """The grant belonged to LAST period. Subtracting it from a fresh counter
    would destroy current-period usage — the exact class of bug this whole area
    is recovering from."""
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000,
                        period=datetime(2020, 1, 1, tzinfo=UTC))
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        db.execute(
            text("UPDATE jobs SET reserved_count = 50, "
                 "reserved_at = TIMESTAMPTZ '2020-01-05 00:00+00' WHERE id = :j"),
            {"j": job_id},
        )
        # A rollover then advanced the period, with fresh usage inside it.
        db.execute(
            text("UPDATE users SET records_used = 10, records_period_start = "
                 "date_trunc('month', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' "
                 "WHERE id = CAST(:u AS uuid)"),
            {"u": user_id},
        )
        db.commit()

    with SyncSessionLocal() as db:
        assert release_quota_reservation(db, job_id) == 0, "stale grant, no refund"

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 10, "new-period usage intact"


def test_release_never_drives_the_counter_negative():
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        # A grant larger than the counter (only reachable if something else
        # already reduced it). The release must clamp, not go negative.
        db.execute(
            text("UPDATE jobs SET reserved_count = 500, reserved_at = NOW() "
                 "WHERE id = :j"),
            {"j": job_id},
        )
        db.commit()

    with SyncSessionLocal() as db:
        release_quota_reservation(db, job_id)

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 0


# ─── Settlement ───────────────────────────────────────────────────────────────

def test_billing_settles_only_the_delta_not_the_whole_amount():
    """Reserved 50, delivered 50 => the bill adds nothing further."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=50)
        db.commit()
        db.expire_all()  # reserve is raw SQL; drop the stale identity-map copy
        assert db.get(User, user_id).records_used == 50

    with SyncSessionLocal() as db:
        _settle(db, user_id, delta=50 - 50)
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 50, "charged once in total"


def test_delivering_fewer_than_reserved_refunds_the_difference():
    """Enrichment can make a row non-actionable between the cap and the bill."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=50)
        db.commit()

    with SyncSessionLocal() as db:
        _settle(db, user_id, delta=42 - 50)   # only 42 actually delivered
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 42


def test_a_job_that_never_reserved_still_bills_its_full_count():
    """Unlimited plans skip the cap entirely, so reserved_count stays 0 and the
    delta expression reduces to the full charge. In-flight jobs at deploy time
    take this same path."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=-1)
        user_id = user.id
        db.commit()

    with SyncSessionLocal() as db:
        _settle(db, user_id, delta=120 - 0)
        db.commit()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 120
