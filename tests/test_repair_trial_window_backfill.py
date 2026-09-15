"""Repair of trial users that migration 088 placed on a calendar window.

Migration 088 backfilled EVERY existing user onto ``[records_period_start, +1
month)``. Registration after 088 gives a trial its own window ending at
``trial_ends_at``, and ``expire_trials`` relies on that. The accounts that were
mid-trial at the deploy got a window running to the 1st instead, so when their
trial expired and the plan dropped to Starter, the Pro-trial usage stayed in the
live window: production read 1,001 / 50.

These tests pin the repair: it closes such a window at the trial end and rolls
it through the SAME shared window SQL the worker charges with, and it refuses
every state where zeroing the counter could destroy usage that belongs to the
post-trial window.

Real Postgres, real settings, no mocks.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from src.api.auth import hash_password
from src.api.quota_window import add_months
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_trial_window_backfill.py"
_spec = importlib.util.spec_from_file_location("repair_trial_window_backfill", _SCRIPT)
rt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rt
_spec.loader.exec_module(rt)


def _now() -> datetime:
    return datetime.now(UTC)


def _legacy_user(db, *, used: int = 1001, trial_ended_days_ago: float = 6.0,
                 plan: str = "starter", limit: int = 50, **overrides) -> User:
    """The exact production shape: calendar window, trial ended inside it."""
    now = _now()
    trial_end = now - timedelta(days=trial_ended_days_ago)
    start = (trial_end - timedelta(days=8)).replace(microsecond=0)
    fields = {
        "id": str(uuid.uuid4()),
        "email": f"trialwin_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        "password_hash": hash_password("TestPass123!"),
        "plan": plan,
        "records_used": used,
        "records_limit": limit,
        "trial_ends_at": trial_end,
        "trial_consumed_at": trial_end,
        "subscription_status": "canceled",
        "records_period_start": start,
        "skip_trace_period_start": start,
        "quota_anchor_at": start,
        "quota_period_start": start,
        "quota_period_end": add_months(start, 1),
    }
    fields.update(overrides)
    user = User(**fields)
    db.add(user)
    db.flush()
    return user


def _config(db, user_id: str) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, name="Trial Window Test",
        county="pierce", state="WA", record_type="probate", fields=[],
        enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    db.flush()
    return config


def _job(db, user: User, config: ScraperConfig, *, status: str = "done",
         billed: int = 0, billed_at: datetime | None = None,
         reserved: int = 0, reserved_at: datetime | None = None) -> Job:
    job = Job(
        id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
        status=status, billed_count=billed, billing_applied_at=billed_at,
        reserved_count=reserved, reserved_at=reserved_at,
        quota_period_start=user.quota_period_start if reserved_at else None,
    )
    db.add(job)
    db.flush()
    return job


def _seed_trial_usage(db, user: User, amount: int) -> None:
    """Ledger rows that exactly back ``records_used``, billed during the trial."""
    config = _config(db, user.id)
    _job(db, user, config, billed=amount,
         billed_at=user.trial_ends_at - timedelta(days=2))


def _run(user_id: str, *, commit: bool) -> rt.Outcome:
    with SyncSessionLocal() as db:
        return rt.repair_user(db, user_id, commit=commit)


def _reload(user_id: str) -> User:
    with SyncSessionLocal() as db:
        user = db.get(User, user_id)
        db.expunge(user)
        return user


# ─── The production defect ────────────────────────────────────────────────────

def test_legacy_trial_window_is_closed_at_trial_end_and_rolled_to_zero():
    with SyncSessionLocal() as db:
        user = _legacy_user(db)
        _seed_trial_usage(db, user, 1001)
        uid, trial_end, anchor = user.id, user.trial_ends_at, user.quota_anchor_at
        old_end = user.quota_period_end
        db.commit()

    outcome = _run(uid, commit=True)

    assert outcome.status == "repaired", outcome
    after = _reload(uid)
    assert after.records_used == 0
    assert after.records_limit == 50 and after.plan == "starter"
    assert after.quota_period_start == trial_end, "new window starts AT the trial end"
    assert after.quota_period_end == old_end, (
        "the anchor grid is unchanged, so the transitional window snaps back to the "
        "legacy boundary"
    )
    assert after.quota_period_end > _now()
    assert after.quota_anchor_at == anchor, "the anchor only moves on the 3 approved events"
    assert after.records_period_start == after.quota_period_start, "mirror stays in lockstep"


def test_ledger_is_never_touched():
    with SyncSessionLocal() as db:
        user = _legacy_user(db, used=104)
        _seed_trial_usage(db, user, 104)
        uid = user.id
        db.commit()
    _run(uid, commit=True)
    with SyncSessionLocal() as db:
        assert db.execute(
            text("SELECT SUM(billed_count) FROM jobs WHERE user_id = CAST(:u AS uuid)"),
            {"u": uid},
        ).scalar() == 104


def test_dry_run_reports_the_exact_outcome_and_writes_nothing():
    with SyncSessionLocal() as db:
        user = _legacy_user(db)
        _seed_trial_usage(db, user, 1001)
        uid, trial_end = user.id, user.trial_ends_at
        db.commit()

    outcome = _run(uid, commit=False)

    assert outcome.status == "would_repair", outcome
    assert outcome.new_period_start == trial_end
    after = _reload(uid)
    assert after.records_used == 1001, "a dry run must roll back"
    assert after.quota_period_start != trial_end


def test_second_run_is_a_no_op():
    with SyncSessionLocal() as db:
        user = _legacy_user(db)
        _seed_trial_usage(db, user, 1001)
        uid = user.id
        db.commit()
    assert _run(uid, commit=True).status == "repaired"
    snapshot = _reload(uid)

    assert _run(uid, commit=True).status == "not_a_candidate"
    again = _reload(uid)
    assert (again.records_used, again.quota_period_start, again.quota_period_end) == (
        snapshot.records_used, snapshot.quota_period_start, snapshot.quota_period_end)


def test_candidate_discovery_finds_only_the_legacy_shape():
    with SyncSessionLocal() as db:
        legacy = _legacy_user(db)
        _seed_trial_usage(db, legacy, 1001)
        # Post-088 registration shape, trial already over and rolled: the live
        # window STARTS at the trial end, so it is not a candidate.
        trial_end = _now() - timedelta(days=1)
        modern = User(
            id=str(uuid.uuid4()),
            email=f"trialwin_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"),
            plan="starter", records_used=3, records_limit=50,
            trial_ends_at=trial_end, trial_consumed_at=trial_end,
            quota_anchor_at=trial_end - timedelta(days=7),
            quota_period_start=trial_end,
            quota_period_end=add_months(trial_end - timedelta(days=7), 1),
        )
        db.add(modern)
        ids = (legacy.id, modern.id)
        db.commit()

    with SyncSessionLocal() as db:
        found = {str(r.id) for r in rt.find_candidates(db)}
    assert ids[0] in found
    assert ids[1] not in found


# ─── Refusals: every state where zeroing could destroy real usage ─────────────

def test_trial_still_running_is_left_alone():
    with SyncSessionLocal() as db:
        user = _legacy_user(db, trial_ended_days_ago=-2, plan="pro", limit=1000, used=300,
                            subscription_status=None)
        _seed_trial_usage(db, user, 300)
        uid = user.id
        db.commit()
    assert _run(uid, commit=True).status == "not_a_candidate"
    assert _reload(uid).records_used == 300


def test_a_paying_customer_is_left_alone():
    with SyncSessionLocal() as db:
        user = _legacy_user(db, plan="pro", limit=1000, first_paid_at=_now() - timedelta(days=5),
                            subscription_status="active")
        _seed_trial_usage(db, user, 1001)
        uid = user.id
        db.commit()
    assert _run(uid, commit=True).status == "not_a_candidate"
    assert _reload(uid).records_used == 1001


def test_a_frozen_account_is_refused_because_its_window_may_not_roll():
    with SyncSessionLocal() as db:
        user = _legacy_user(db, subscription_status="unpaid")
        _seed_trial_usage(db, user, 1001)
        uid, end = user.id, user.quota_period_end
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "refused_not_rollable", outcome
    after = _reload(uid)
    assert after.records_used == 1001 and after.quota_period_end == end


def test_usage_billed_after_the_trial_end_is_refused():
    """That usage belongs to the post-trial window; zeroing would erase it."""
    with SyncSessionLocal() as db:
        user = _legacy_user(db, used=1011)
        config = _config(db, user.id)
        _job(db, user, config, billed=1001, billed_at=user.trial_ends_at - timedelta(days=2))
        _job(db, user, config, billed=10, billed_at=user.trial_ends_at + timedelta(hours=1))
        uid = user.id
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "refused_post_trial_usage", outcome
    assert _reload(uid).records_used == 1011


def test_a_reservation_taken_after_the_trial_end_is_refused():
    with SyncSessionLocal() as db:
        user = _legacy_user(db, used=1006)
        config = _config(db, user.id)
        _job(db, user, config, billed=1001, billed_at=user.trial_ends_at - timedelta(days=2))
        _job(db, user, config, status="failed", reserved=5,
             reserved_at=user.trial_ends_at + timedelta(hours=1))
        uid = user.id
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "refused_post_trial_usage", outcome
    assert _reload(uid).records_used == 1006


def test_a_job_still_in_flight_is_refused():
    """Its reserve or settlement could land on either side of the repair."""
    with SyncSessionLocal() as db:
        user = _legacy_user(db)
        config = _config(db, user.id)
        _job(db, user, config, billed=1001, billed_at=user.trial_ends_at - timedelta(days=2))
        _job(db, user, config, status="scraping")
        uid = user.id
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "refused_job_in_flight", outcome
    assert _reload(uid).records_used == 1001


def test_a_counter_the_ledger_does_not_explain_is_refused():
    """The repair zeroes trial usage. If the counter is not exactly that, stop."""
    with SyncSessionLocal() as db:
        user = _legacy_user(db, used=1001)
        _seed_trial_usage(db, user, 990)
        uid = user.id
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "refused_counter_mismatch", outcome
    assert _reload(uid).records_used == 1001


def test_a_billed_count_without_a_billing_timestamp_is_refused():
    """It cannot be placed on either side of the trial end, so stop."""
    with SyncSessionLocal() as db:
        user = _legacy_user(db, used=1001)
        config = _config(db, user.id)
        _job(db, user, config, billed=1001, billed_at=user.trial_ends_at - timedelta(days=2))
        _job(db, user, config, billed=7, billed_at=None)
        uid = user.id
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "refused_malformed_ledger", outcome
    assert _reload(uid).records_used == 1001


def test_a_user_locked_by_a_charging_statement_is_skipped_not_blocked():
    with SyncSessionLocal() as db:
        user = _legacy_user(db)
        _seed_trial_usage(db, user, 1001)
        uid = user.id
        db.commit()

    holder = SyncSessionLocal()
    try:
        holder.execute(text("SELECT id FROM users WHERE id = CAST(:u AS uuid) FOR UPDATE"),
                       {"u": uid})
        outcome = _run(uid, commit=True)
    finally:
        holder.rollback()
        holder.close()
    assert outcome.status == "skipped_locked", outcome
    assert _reload(uid).records_used == 1001


def test_a_conversion_that_commits_first_wins():
    """Checkout stamps first_paid_at and clears trial_ends_at under the row lock.

    The repair re-reads the row AFTER taking its own lock, so a conversion that
    committed in between is seen and left alone instead of being overwritten.
    """
    with SyncSessionLocal() as db:
        user = _legacy_user(db)
        _seed_trial_usage(db, user, 1001)
        uid = user.id
        db.commit()

    with SyncSessionLocal() as db:
        db.execute(
            text("UPDATE users SET first_paid_at = clock_timestamp(), trial_ends_at = NULL, "
                 "plan = 'pro', records_limit = 1000 WHERE id = CAST(:u AS uuid)"),
            {"u": uid},
        )
        db.commit()
    outcome = _run(uid, commit=True)
    assert outcome.status == "not_a_candidate", outcome
    after = _reload(uid)
    assert after.plan == "pro" and after.records_used == 1001


def test_other_accounts_are_untouched():
    with SyncSessionLocal() as db:
        target = _legacy_user(db)
        _seed_trial_usage(db, target, 1001)
        bystander = _legacy_user(db, used=40, trial_ended_days_ago=6)
        _seed_trial_usage(db, bystander, 40)
        tid, bid = target.id, bystander.id
        db.commit()
    assert _run(tid, commit=True).status == "repaired"
    assert _reload(bid).records_used == 40
