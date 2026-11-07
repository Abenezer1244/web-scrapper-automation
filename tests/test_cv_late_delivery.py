"""Late delivery of held code violations (src/workers/cv_late_delivery.py): real Postgres.

Owner decision 2026-10-30 (option 2): a held code violation that a background sweep
completes is delivered and billed at once, without waiting for the next run. Pinned here:
  - a complete held row is delivered once: mark removed, late_delivery stamped, one record
    charged against the current window, the run's own numbers untouched;
  - it then reads as a lead in its run (Results new list), and leaves the held list;
  - nothing happens to a row that is incomplete, a duplicate, superseded, already late
    delivered, whose claim a later run took, whose run is not done, of another account;
  - no quota left, or a frozen/ended account: nothing written, the row stays held;
  - an unlimited plan is charged too; same-run siblings stay held; the kill switch;
  - two ticks at once charge once.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, NOT_QUALIFIED
from src.api.lead_qualification import HOLD_KEY, POLICY_CODE_VIOLATION
from src.config import settings
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig
from src.privacy.authorize import build_snapshot
from src.workers import cv_late_delivery as late
from tests.test_lead_qualification import seattle


async def _config(db, user) -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King code violations", county="king",
        state="WA", record_type="code_violation", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user, cfg, *, status="done") -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status=status,
               trigger="manual", record_count=0, billed_count=0))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, spec: dict, **kw) -> str:
    rid = str(uuid.uuid4())
    fields = {k: v for k, v in spec.items() if k not in ("phone", "email")}
    fields.update(kw)
    fields.setdefault("is_duplicate", False)
    fields.setdefault("dedup_hash", uuid.uuid4().hex)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, **fields))
    await db.commit()
    return rid


def _held(**kw) -> dict:
    """A Seattle case the gate held; complete now unless overridden."""
    spec = seattle(**kw)
    spec["enrichment_data"] = {**spec["enrichment_data"], DELIVERY_EXCLUDED_KEY: NOT_QUALIFIED,
                               HOLD_KEY: {"reasons": ["missing_owner"], "policy": POLICY_CODE_VIOLATION}}
    return spec


async def _held_row(db, user, job, spec=None, *, claim=True, **kw) -> str:
    rid = await _row(db, job, user.id, spec or _held(), **kw)
    if claim:
        h = (await db.execute(text("SELECT dedup_hash FROM results WHERE id = :i"), {"i": rid})).scalar()
        db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=user.id, dedup_hash=h,
                               first_result_id=rid, first_job_id=job))
        await db.commit()
    return rid


async def _state(db, rid):
    return (await db.execute(text(
        "SELECT enrichment_data, is_duplicate FROM results WHERE id = :i"), {"i": rid})).one()


async def _used(db, user) -> int:
    return (await db.execute(text("SELECT records_used FROM users WHERE id = :u"),
                             {"u": user.id})).scalar()


async def _job_numbers(db, job):
    return tuple((await db.execute(text(
        "SELECT billed_count, record_count, billing_applied_at, breakdown_new, breakdown_not_qualified "
        "FROM jobs WHERE id = :j"), {"j": job})).one())


def _tick() -> dict:
    return late.deliver_completed_held_rows()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)


@pytest.mark.asyncio
async def test_a_completed_held_row_is_delivered_and_billed_once(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    used, numbers = await _used(db, business_user), await _job_numbers(db, job)

    stats = _tick()

    assert stats["delivered"] == 1
    ed, _ = await _state(db, rid)
    assert DELIVERY_EXCLUDED_KEY not in ed
    assert ed[late.LATE_KEY]["policy"] == POLICY_CODE_VIOLATION and ed[late.LATE_KEY]["delivered_at"]
    assert ed[HOLD_KEY]["reasons"] == ["missing_owner"]          # why it was held stays on record
    assert await _used(db, business_user) == used + 1
    assert await _job_numbers(db, job) == numbers                 # the run's numbers never move

    again = _tick()
    assert again["candidates"] == 0 and await _used(db, business_user) == used + 1


@pytest.mark.asyncio
async def test_the_delivered_row_reads_as_a_lead_of_its_run(client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await db.execute(text("UPDATE jobs SET billing_applied_at = now(), export_key = 'exports/x.csv' "
                          "WHERE id = :j"), {"j": job})
    await db.commit()
    rid = await _held_row(db, business_user, job)
    auth = {"Authorization": f"Bearer {business_token}"}

    def ids(page):
        return [i["id"] for i in page["items"]]

    held = (await client.get(f"/jobs/{job}/results", params={"category": "not_qualified"}, headers=auth)).json()
    assert ids(held) == [rid]
    _tick()
    new = (await client.get(f"/jobs/{job}/results", params={"category": "new"}, headers=auth)).json()
    held = (await client.get(f"/jobs/{job}/results", params={"category": "not_qualified"}, headers=auth)).json()
    assert ids(new) == [rid] and ids(held) == []


@pytest.mark.asyncio
async def test_rows_that_must_not_be_delivered_are_left_alone(db, business_user, starter_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    running = await _job(db, business_user, cfg, status="enriching")
    other_cfg = await _config(db, starter_user)
    other_job = await _job(db, starter_user, other_cfg)
    u = business_user
    rows = {
        "incomplete": await _held_row(db, u, job, _held(mailing_address=None)),
        "duplicate": await _held_row(db, u, job, is_duplicate=True, duplicate_reason="same_run"),
        "superseded": await _held_row(db, u, job, is_duplicate=True, duplicate_reason="superseded"),
        "no_claim": await _held_row(db, u, job, claim=False),
        "run_not_done": await _held_row(db, u, running),
        "unstamped": await _held_row(db, u, job, {**_held(), "enrichment_data": {
            k: v for k, v in _held()["enrichment_data"].items() if k != "qualification_policy"}}),
        "over_quota": await _held_row(db, u, job, {**_held(), "enrichment_data": {
            **_held()["enrichment_data"], DELIVERY_EXCLUDED_KEY: "over_quota"}}),
        "already_late": await _held_row(db, u, job, {**_held(), "enrichment_data": {
            **_held()["enrichment_data"], late.LATE_KEY: {"delivered_at": "x"}}}),
    }
    # A row whose account is not its run's account.
    rows["foreign_tenant"] = await _held_row(db, u, other_job)
    used = await _used(db, u)

    stats = _tick()

    assert stats["delivered"] == 0
    for name, rid in rows.items():
        ed, _ = await _state(db, rid)
        assert ed.get(DELIVERY_EXCLUDED_KEY) in (NOT_QUALIFIED, "over_quota"), name
    assert await _used(db, u) == used


@pytest.mark.asyncio
async def test_a_claim_a_later_run_took_is_never_delivered_again(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    later = await _job(db, business_user, cfg)
    newer = await _row(db, later, business_user.id, seattle())
    await db.execute(text("UPDATE delivered_records SET first_result_id = :n WHERE first_result_id = :o"),
                     {"n": newer, "o": rid})
    await db.commit()
    used = await _used(db, business_user)

    assert _tick()["delivered"] == 0
    ed, _ = await _state(db, rid)
    assert ed[DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED and await _used(db, business_user) == used


@pytest.mark.asyncio
async def test_a_claim_taken_after_discovery_is_caught_under_the_lock(db, business_user):
    """Discovery is only a read: a later run's claim transfer can land between it and the
    row's transaction, so the holder is decided again under the claim lock."""
    from src.db.session import system_sync_session

    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    with system_sync_session() as s:
        [found] = s.execute(text(late._CANDIDATES_SQL), {"batch": 10, "per_account": 10}).mappings().all()
        s.rollback()
    later = await _job(db, business_user, cfg)
    newer = await _row(db, later, business_user.id, seattle())
    await db.execute(text("UPDATE delivered_records SET first_result_id = :n WHERE first_result_id = :o"),
                     {"n": newer, "o": rid})
    await db.commit()
    used = await _used(db, business_user)

    with system_sync_session() as s:
        assert late._deliver_one(s, found, build_snapshot([])) == "stale"
    assert (await _state(db, rid))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED
    assert await _used(db, business_user) == used


@pytest.mark.asyncio
async def test_no_quota_left_keeps_it_held(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    await db.execute(text("UPDATE users SET records_used = records_limit WHERE id = :u"), {"u": business_user.id})
    await db.commit()

    stats = _tick()

    assert (stats["delivered"], stats["no_quota"]) == (0, 1)
    ed, _ = await _state(db, rid)
    assert ed[DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED and late.LATE_KEY not in ed
    assert await _used(db, business_user) == 5000


@pytest.mark.asyncio
async def test_an_ended_account_is_never_charged(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    await db.execute(text("UPDATE users SET entitlement_ends_at = now() - interval '1 day' WHERE id = :u"),
                     {"u": business_user.id})
    await db.commit()
    used = await _used(db, business_user)

    stats = _tick()

    assert (stats["delivered"], stats["blocked"]) == (0, 1)
    assert (await _state(db, rid))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED
    assert await _used(db, business_user) == used


@pytest.mark.asyncio
async def test_an_unlimited_plan_is_delivered_and_charged(db, business_user):
    await db.execute(text("UPDATE users SET records_limit = -1, plan = 'agency' WHERE id = :u"),
                     {"u": business_user.id})
    await db.commit()
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _held_row(db, business_user, job)
    used = await _used(db, business_user)

    assert _tick()["delivered"] == 1
    assert await _used(db, business_user) == used + 1


@pytest.mark.asyncio
async def test_same_run_siblings_stay_held(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    h = (await db.execute(text("SELECT dedup_hash FROM results WHERE id = :i"), {"i": rid})).scalar()
    sibling = await _row(db, job, business_user.id, _held(), is_duplicate=True,
                         duplicate_reason="same_run", dedup_hash=h)

    assert _tick()["delivered"] == 1
    assert (await _state(db, sibling))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED


@pytest.mark.asyncio
async def test_two_ticks_at_once_charge_once(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    for _ in range(5):
        await _held_row(db, business_user, job)
    used = await _used(db, business_user)
    results: list[dict] = []
    gate = threading.Barrier(2)

    def run():
        gate.wait()
        results.append(_tick())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert sum(r["delivered"] for r in results) == 5
    assert await _used(db, business_user) == used + 5


@pytest.mark.asyncio
async def test_one_accounts_backlog_never_crowds_out_another(db, business_user, starter_user, monkeypatch):
    """Codex P2: oldest-first over everyone let an account with no quota hold every slot."""
    await db.execute(text("UPDATE users SET records_used = records_limit WHERE id = :u"), {"u": business_user.id})
    await db.execute(text("UPDATE users SET plan = 'business', records_limit = 5000, records_used = 0 "
                          "WHERE id = :u"), {"u": starter_user.id})
    await db.commit()
    full_cfg, other_cfg = await _config(db, business_user), await _config(db, starter_user)
    full_job, other_job = await _job(db, business_user, full_cfg), await _job(db, starter_user, other_cfg)
    for _ in range(6):
        await _held_row(db, business_user, full_job)           # older, and cannot be charged
    theirs = await _held_row(db, starter_user, other_job)

    stats = late.deliver_completed_held_rows(batch=3)

    assert stats["delivered"] == 1 and stats["no_quota"] == 1   # one try for the full account, then skipped
    assert DELIVERY_EXCLUDED_KEY not in (await _state(db, theirs))[0]


@pytest.mark.asyncio
async def test_a_failure_after_the_grant_rolls_the_charge_back(db, business_user, monkeypatch):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)
    used = await _used(db, business_user)
    monkeypatch.setattr(late, "_DELIVER_SQL", "UPDATE results SET no_such_column = 1")

    stats = _tick()

    assert stats["errors"] == 1 and stats["delivered"] == 0
    assert await _used(db, business_user) == used               # the grant went with the rollback
    assert (await _state(db, rid))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED


@pytest.mark.asyncio
async def test_a_full_account_whose_window_rolled_is_charged_in_the_new_window(db, business_user):
    """The grant is judged at the post-lock clock: a window that ended resets the counter."""
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _held_row(db, business_user, job)
    await db.execute(text(
        "UPDATE users SET records_used = records_limit, quota_period_start = now() - interval '40 days', "
        "quota_period_end = now() - interval '10 days', quota_anchor_at = now() - interval '40 days' "
        "WHERE id = :u"), {"u": business_user.id})
    await db.commit()

    assert _tick()["delivered"] == 1
    assert await _used(db, business_user) == 1


@pytest.mark.asyncio
async def test_the_kill_switch_delivers_and_bills_nothing(db, business_user, monkeypatch):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", False)
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _held_row(db, business_user, job)

    stats = _tick()

    assert stats["skipped"] == "CV_LATE_DELIVERY_ENABLED is off" and stats["candidates"] == 0
    assert (await _state(db, rid))[0][DELIVERY_EXCLUDED_KEY] == NOT_QUALIFIED


def test_the_task_is_registered_and_scheduled():
    from celery.schedules import crontab

    from src.workers import app
    from src.workers.scheduler import app as beat_app

    entry = beat_app.conf.beat_schedule["deliver-completed-held-code-violations"]
    assert isinstance(entry["schedule"], crontab) and sorted(entry["schedule"].minute) == [5, 25, 45]
    assert "src.workers.cv_late_delivery" in app.conf.include
    assert entry["task"] in app.tasks
