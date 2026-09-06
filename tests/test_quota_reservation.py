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
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.api.auth import hash_password
from src.api.quota_window import (
    add_months,
    reservation_is_current_sql,
    window_cte_sql,
    window_set_sql,
)
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal


def _month_start(dt: datetime | None = None) -> datetime:
    dt = dt or datetime.now(UTC)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _mk_user(db, *, used: int = 0, limit: int = 1000,
             period: datetime | None = None,
             window_end: datetime | None = None) -> User:
    """``period`` is the start of the user's ENTITLEMENT WINDOW (migration 088).

    ``window_end`` defaults to one month later. Pass it explicitly to place a
    boundary somewhere a test needs it — that is how the reservation-crossing
    cases below put a window end between a reserve and its settlement.
    """
    start = period or _month_start()
    user = User(
        id=str(uuid.uuid4()),
        email=f"resv_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=used,
        records_limit=limit,
        records_period_start=start,
        skip_trace_period_start=start,
        quota_anchor_at=start,
        quota_period_start=start,
        quota_period_end=window_end or add_months(start, 1),
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


# The reserve and settle statements are assembled from the SAME shared builders
# production uses (src/api/quota_window.py), not hand-copied. A test that carries
# its own transcription of the rule proves only that the transcription is
# self-consistent — which is exactly how seven copies of the old period rule came
# to disagree, two of them wrongly, without a single failing test.

_RESERVE_SQL = (
    "WITH cur AS ("
    "  SELECT u.id, u.records_used, u.records_limit, u.quota_anchor_at,"
    "         u.quota_period_start, u.quota_period_end, u.subscription_status,"
    "         u.entitlement_grace_ends_at, u.entitlement_ends_at,"
    "         u.pending_plan, u.pending_records_limit"
    "  FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE"
    "), w AS ("
    "  SELECT cur.*, " + window_cte_sql("", ":at") + " FROM cur"
    "), g AS ("
    "  SELECT w.*, LEAST(:want, GREATEST(0, eff_limit - base)) AS granted FROM w"
    "), claim AS ("
    "  UPDATE jobs SET reserved_count = (SELECT granted FROM g),"
    "                  reserved_at = CAST(:at AS timestamptz),"
    "                  quota_period_start = (SELECT new_start FROM g)"
    "  WHERE id = :jid AND reserved_at IS NULL"
    "  RETURNING reserved_count"
    "), charge AS ("
    "  UPDATE users u SET"
    "    records_used = g.base + COALESCE((SELECT reserved_count FROM claim), 0),"
    + window_set_sql("g")
    + "  FROM g WHERE u.id = g.id"
    ") SELECT COALESCE((SELECT reserved_count FROM claim), -1)"
)

_SETTLE_SQL = (
    "WITH cur AS ("
    "  SELECT u.id, u.records_used, u.records_limit, u.quota_anchor_at,"
    "         u.quota_period_start, u.quota_period_end, u.subscription_status,"
    "         u.entitlement_grace_ends_at, u.entitlement_ends_at,"
    "         u.pending_plan, u.pending_records_limit, u.records_period_start,"
    "         j.quota_period_start AS job_window, j.reserved_at AS job_reserved_at,"
    "         j.reserved_count AS job_reserved"
    "  FROM users u JOIN jobs j ON j.user_id = u.id"
    "  WHERE j.id = :jid FOR UPDATE OF u"
    "), w AS ("
    "  SELECT cur.*, " + window_cte_sql("", ":at") + " FROM cur"
    "), s AS ("
    "  SELECT w.*, CASE WHEN "
    + reservation_is_current_sql(
        job_window="job_window",
        job_reserved_at="job_reserved_at",
        user_window="quota_period_start",
        user_records_period_start="records_period_start",
    )
    + "    THEN job_reserved ELSE 0 END AS applied_reserved"
    "  FROM w"
    ") UPDATE users u SET"
    "    records_used = GREATEST(0, s.base + (:billable - s.applied_reserved)),"
    + window_set_sql("s")
    + "  FROM s WHERE u.id = s.id"
    "  RETURNING s.applied_reserved"
)


def _reserve(db, job_id: str, user_id: str, want: int, at: datetime | None = None) -> int:
    """The atomic reserve-and-charge the plan cap performs (workers/tasks.py).

    Returns the granted amount, or -1 when this job had already reserved.
    """
    at = at or db.execute(text("SELECT clock_timestamp()")).scalar()
    return db.execute(
        text(_RESERVE_SQL),
        {"want": want, "uid": user_id, "jid": job_id, "at": at},
    ).scalar()


def _settle_job(db, job_id: str, billable: int, at: datetime | None = None) -> int:
    """The delta settlement the billing block applies. Returns what it netted off."""
    at = at or db.execute(text("SELECT clock_timestamp()")).scalar()
    return db.execute(
        text(_SETTLE_SQL),
        {"jid": job_id, "billable": billable, "at": at},
    ).scalar()


def _settle(db, user_id: str, delta: int) -> None:
    """Legacy shim for the tests below that charge a bare delta with no job."""
    from src.api.quota_window import window_cte_sql as _w
    from src.api.quota_window import window_set_sql as _ws

    db.execute(
        text(
            "WITH cur AS ("
            "  SELECT u.id, u.records_used, u.records_limit, u.quota_anchor_at,"
            "         u.quota_period_start, u.quota_period_end, u.subscription_status,"
            "         u.entitlement_grace_ends_at, u.entitlement_ends_at,"
            "         u.pending_plan, u.pending_records_limit"
            "  FROM users u WHERE u.id = CAST(:uid AS uuid) FOR UPDATE"
            "), w AS (SELECT cur.*, " + _w("", ":at") + " FROM cur"
            ") UPDATE users u SET"
            "    records_used = GREATEST(0, w.base + :delta),"
            + _ws("w")
            + "  FROM w WHERE u.id = w.id"
        ),
        {"delta": delta, "uid": user_id, "at": datetime.now(UTC)},
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


def test_TRULY_concurrent_reservations_serialise_on_the_user_row():
    """The sequential test above proves the arithmetic; this proves the LOCK.

    Two threads race to reserve from a 100-record remainder, each wanting 80.
    `SELECT ... FOR UPDATE` in the reserve CTE must make the loser block until
    the winner commits and then re-read the already-decremented remainder
    (READ COMMITTED re-evaluates a locked row against the newer version). If the
    lock did not hold, both would compute 80 and the user would be handed 160.
    """
    import threading

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=900, limit=1000)      # 100 remaining
        config = _mk_config(db, user.id)
        job_ids = [_mk_job(db, user.id, config.id).id for _ in range(2)]
        user_id = user.id
        db.commit()

    start = threading.Barrier(2)
    grants: dict[str, int] = {}
    errors: list[Exception] = []

    def worker(job_id: str) -> None:
        try:
            with SyncSessionLocal() as db:
                start.wait(timeout=10)      # maximise overlap
                grants[job_id] = _reserve(db, job_id, user_id, want=80)
                db.commit()
        except Exception as exc:            # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(j,)) for j in job_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"reservation raised under concurrency: {errors}"
    assert sorted(grants.values()) == [20, 80], (
        f"one job must win 80 and the other get only the remaining 20: {grants}"
    )

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 1000, (
            "the two jobs together must never exceed the plan"
        )


# ─── Reconciliation sweep (catches paths no release call covers) ──────────────

def test_sweep_releases_a_reservation_stranded_by_an_external_cancel():
    """An external cancel writes only jobs.status — no release code runs.

    Enumerating call sites is the fragile fix; the sweep works by STATE, so a
    grant stranded by ANY path is returned within one beat interval.
    """
    from src.workers.tasks_helpers.status import sweep_stranded_quota_reservations

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=75)
        db.commit()
        # Something outside the task terminalizes it, touching only status.
        db.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"),
                   {"j": job_id})
        db.commit()

    assert sweep_stranded_quota_reservations() >= 1

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 0
        assert db.get(Job, job_id).reserved_count == 0


def test_sweep_leaves_a_live_job_alone():
    """A job still running holds its reservation legitimately."""
    from src.workers.tasks_helpers.status import sweep_stranded_quota_reservations

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)   # status='enriching'
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=75)
        db.commit()

    sweep_stranded_quota_reservations()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 75, "live job keeps its grant"


def test_sweep_leaves_a_billed_job_alone():
    from src.workers.tasks_helpers.status import sweep_stranded_quota_reservations

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        _reserve(db, job_id, user_id, want=75)
        db.commit()
        db.execute(
            text("UPDATE jobs SET status='done', billing_applied_at=NOW(), "
                 "billed_count=75 WHERE id = :j"),
            {"j": job_id},
        )
        db.commit()

    sweep_stranded_quota_reservations()

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 75, "a billed job owns its charge"


def test_release_is_month_scoped_not_merely_less_than_or_equal():
    """The guard compares MONTHS. A reservation made mid-month against a period
    that has since rolled must not be refunded out of the new period."""
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=200, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()
        # Grant belongs to a PREVIOUS month; the user's period is current.
        db.execute(
            text("UPDATE jobs SET reserved_count = 90, "
                 "reserved_at = TIMESTAMPTZ '2020-06-15 12:00+00' WHERE id = :j"),
            {"j": job_id},
        )
        db.commit()

    with SyncSessionLocal() as db:
        assert release_quota_reservation(db, job_id) == 0

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 200, "current period untouched"


# ─── Reservations that cross an entitlement boundary ─────────────────────────
#
# The handoff's question: a job reserves 200 at 11:59, the window ends at 12:00,
# the job settles at 12:05. Which window owns those records?
#
# The OLD test asked "same calendar month?", which is only accidentally right
# while every window starts on the 1st. With a 20th anchor, a job reserving on
# the 19th and settling on the 21st read as "same period" — so settlement netted
# its grant off a counter the 20th's rollover had already zeroed, and the
# delivered records ended up charged to NOBODY. These pin the real rule.

def _job_window(db, job_id: str):
    return db.execute(
        text("SELECT quota_period_start FROM jobs WHERE id = :j"), {"j": job_id}
    ).scalar()


def test_a_reservation_records_the_window_it_was_charged_to():
    """Without this column the question cannot even be asked."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        _reserve(db, job_id, user_id, want=200)
        db.commit()
        assert _job_window(db, job_id) == db.get(User, user_id).quota_period_start


def test_a_boundary_between_reserve_and_settle_charges_the_LIVE_window_in_full():
    """Reserve 200 just before the boundary; settle 200 just after it.

    The rollover zeroed the counter, taking the 200 with it. Netting
    (billable - reserved) = 0 against the new window would deliver 200 records
    charged to nobody. The leads are being delivered NOW, so the live window
    carries them in full.
    """
    boundary = datetime.now(UTC) - timedelta(minutes=5)
    with SyncSessionLocal() as db:
        # A window that ended five minutes ago; the reservation was taken inside it.
        user = _mk_user(
            db, used=0, limit=1000,
            period=boundary - timedelta(days=30), window_end=boundary,
        )
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        granted = _reserve(db, job_id, user_id, want=200,
                           at=boundary - timedelta(minutes=1))
        db.commit()
    assert granted == 200

    with SyncSessionLocal() as db:
        # Settling now: the statement rolls the window in the same breath.
        applied = _settle_job(db, job_id, billable=200)
        db.commit()
        fresh = db.get(User, user_id)

    assert applied == 0, "a grant from the previous window must not be netted off"
    assert fresh.records_used == 200, "the live window carries the delivery in full"
    assert fresh.quota_period_start >= boundary, "and the window did roll"


def test_a_reservation_inside_the_live_window_still_settles_as_a_delta():
    """The ordinary case must be untouched: reserve 200, deliver 200, net zero."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        _reserve(db, job_id, user_id, want=200)
        db.commit()
        assert db.get(User, user_id).records_used == 200

    with SyncSessionLocal() as db:
        applied = _settle_job(db, job_id, billable=200)
        db.commit()
        assert applied == 200
        assert db.get(User, user_id).records_used == 200, "charged exactly once"


def test_delivering_fewer_records_than_reserved_refunds_the_difference():
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        _reserve(db, job_id, user_id, want=200)
        db.commit()

    with SyncSessionLocal() as db:
        _settle_job(db, job_id, billable=150)
        db.commit()
        assert db.get(User, user_id).records_used == 150


def test_releasing_a_reservation_from_a_rolled_window_refunds_nothing():
    """The mirror image of the settlement bug.

    Refunding into a window the grant was never added to would DESTROY
    current-window usage — the exact class of bug PR #223 exists to fix. The
    bookkeeping is retired instead, so the 5-minute sweep stops re-examining the
    job for the rest of its life.
    """
    from src.workers.tasks_helpers.status import release_quota_reservation

    boundary = datetime.now(UTC) - timedelta(minutes=5)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, used=0, limit=1000,
            period=boundary - timedelta(days=30), window_end=boundary,
        )
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        _reserve(db, job_id, user_id, want=200, at=boundary - timedelta(minutes=1))
        db.commit()

    # The window rolls (a beat pass, or another job), then this job fails.
    with SyncSessionLocal() as db:
        db.execute(
            text(
                "UPDATE users SET records_used = 40, "
                "quota_period_start = :s, quota_period_end = :e, "
                "records_period_start = :s WHERE id = CAST(:u AS uuid)"
            ),
            {"s": boundary, "e": add_months(boundary, 1), "u": user_id},
        )
        db.commit()

    with SyncSessionLocal() as db:
        freed = release_quota_reservation(db, job_id)

    with SyncSessionLocal() as db:
        fresh = db.get(User, user_id)
        job_row = db.execute(
            text("SELECT reserved_at, reserved_count FROM jobs WHERE id = :j"),
            {"j": job_id},
        ).one()

    assert freed == 0, "there is nothing left to refund"
    assert fresh.records_used == 40, "current-window usage must not be eaten"
    assert job_row.reserved_at is None and job_row.reserved_count == 0, (
        "retired, so the sweep does not re-examine it forever"
    )


def test_releasing_a_reservation_inside_the_live_window_still_refunds():
    """The ordinary failure path must be untouched."""
    from src.workers.tasks_helpers.status import release_quota_reservation

    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        _reserve(db, job_id, user_id, want=200)
        db.commit()
        assert db.get(User, user_id).records_used == 200

    with SyncSessionLocal() as db:
        freed = release_quota_reservation(db, job_id)

    with SyncSessionLocal() as db:
        assert freed == 200
        assert db.get(User, user_id).records_used == 0


def test_a_legacy_reservation_with_no_window_keeps_its_calendar_behaviour():
    """A job that reserved BEFORE migration 088 has a NULL jobs.quota_period_start.

    Those in-flight jobs must settle exactly as they did on the day they started
    — under the calendar-month comparison they were written with — the same
    deploy seam migration 087 used for reserved_count.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, used=0, limit=1000)
        config = _mk_config(db, user.id)
        job = _mk_job(db, user.id, config.id)
        user_id, job_id = user.id, job.id
        db.commit()

    with SyncSessionLocal() as db:
        _reserve(db, job_id, user_id, want=120)
        # Erase the new column to reproduce a pre-088 in-flight job exactly.
        db.execute(
            text("UPDATE jobs SET quota_period_start = NULL WHERE id = :j"),
            {"j": job_id},
        )
        db.commit()

    with SyncSessionLocal() as db:
        applied = _settle_job(db, job_id, billable=120)
        db.commit()
        assert applied == 120, "the legacy month comparison still nets its grant"
        assert db.get(User, user_id).records_used == 120


def test_two_workers_rolling_the_same_user_cannot_double_reset():
    """Concurrent rollover.

    Both sessions meet an ended window and both charge. The rollover is a clause
    inside the statement that already holds the row, so the first commit makes
    the predicate false and the second simply adds its own delta to the counter
    the first established — no double reset, no lost usage, no over-allocation.
    """
    boundary = datetime.now(UTC) - timedelta(minutes=1)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, used=900, limit=1000,
            period=boundary - timedelta(days=30), window_end=boundary,
        )
        config = _mk_config(db, user.id)
        job_a = _mk_job(db, user.id, config.id)
        job_b = _mk_job(db, user.id, config.id)
        user_id, a_id, b_id = user.id, job_a.id, job_b.id
        db.commit()

    with SyncSessionLocal() as sess_a, SyncSessionLocal() as sess_b:
        granted_a = _reserve(sess_a, a_id, user_id, want=300)
        sess_a.commit()
        granted_b = _reserve(sess_b, b_id, user_id, want=300)
        sess_b.commit()

    with SyncSessionLocal() as db:
        fresh = db.get(User, user_id)

    # The window rolled ONCE: the stale 900 is gone, and both grants are honoured
    # against the fresh 1,000 rather than one being lost or duplicated.
    assert granted_a == 300
    assert granted_b == 300
    assert fresh.records_used == 600
    assert fresh.quota_period_start >= boundary
