"""Late delivery notices (Phase C3): every delivered row announced exactly once, per account.

Delivery stamps late_delivery.notified = false; notify_late_deliveries (every tick) adds ONE
notification per account and marks the rows notified in the same system-session
transaction, under an advisory lock on the account. Real Postgres, real notifications rows.
"""
from __future__ import annotations

import json
import threading

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.privacy.authorize import build_snapshot
from src.workers import cv_late_delivery as late
from src.workers import notification_emit
from tests.test_cv_late_delivery import (  # noqa: F401 -- shared fixtures
    _config,
    _held_row,
    _job,
    _row,
)
from tests.test_lead_qualification import seattle


async def _notices(db, user) -> list:
    return (await db.execute(text(
        "SELECT job_id::text AS job_id, detail FROM notifications WHERE user_id = :u AND type = 'late_delivery' "
        "ORDER BY created_at"), {"u": user.id})).mappings().all()


async def _pending(db, user) -> int:
    return (await db.execute(text(
        "SELECT count(*) FROM results WHERE user_id = :u "
        "AND enrichment_data::jsonb->'late_delivery'->>'notified' = 'false'"), {"u": user.id})).scalar()


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)


@pytest.mark.asyncio
async def test_a_tick_delivers_and_announces_once(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _held_row(db, business_user, job)
    await _held_row(db, business_user, job, property_address="2 SECOND ST")

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 2 and stats["notified_accounts"] == 1
    [notice] = await _notices(db, business_user)
    assert notice["detail"] == {"count": 2, "job_ids": [job]} and notice["job_id"] == job
    assert await _pending(db, business_user) == 0

    late.deliver_completed_held_rows()
    assert len(await _notices(db, business_user)) == 1           # never announced twice


@pytest.mark.asyncio
async def test_rows_a_crashed_tick_never_announced_are_announced_next_tick(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    stamp = {"delivered_at": "2026-10-31T02:25:00+00:00", "policy": "code_violation_v1", "notified": False}
    rid = await _row(db, job, business_user.id, seattle())
    await db.execute(text("UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                          "jsonb_build_object('late_delivery', CAST(:s AS jsonb)))::json WHERE id = :i"),
                     {"s": json.dumps(stamp), "i": rid})
    await db.commit()

    assert late.notify_late_deliveries() == 1
    [notice] = await _notices(db, business_user)
    assert notice["detail"]["count"] == 1 and await _pending(db, business_user) == 0


@pytest.mark.asyncio
async def test_a_pending_stamp_outside_done_code_violation_runs_is_never_touched(db, business_user):
    """Codex C3 P2: the per-account rows use the same scope as the account discovery."""
    cfg = await _config(db, business_user)
    running = await _job(db, business_user, cfg, status="enriching")
    stamp = {"delivered_at": "2026-10-31T02:25:00+00:00", "policy": "code_violation_v1", "notified": False}
    rid = await _row(db, running, business_user.id, seattle())
    await db.execute(text("UPDATE results SET enrichment_data = (enrichment_data::jsonb || "
                          "jsonb_build_object('late_delivery', CAST(:s AS jsonb)))::json WHERE id = :i"),
                     {"s": json.dumps(stamp), "i": rid})
    await db.commit()

    assert late.notify_late_deliveries() == 0
    assert await _notices(db, business_user) == [] and await _pending(db, business_user) == 1


@pytest.mark.asyncio
async def test_notices_turned_off_mark_the_rows_and_add_nothing(db, business_user):
    await db.execute(text("UPDATE users SET notification_prefs = '{\"late_delivery\": false}' WHERE id = :u"),
                     {"u": business_user.id})
    await db.commit()
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _held_row(db, business_user, job)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 1 and stats["notified_accounts"] == 0
    assert await _notices(db, business_user) == [] and await _pending(db, business_user) == 0


@pytest.mark.asyncio
async def test_each_account_gets_its_own_notice_and_a_multi_run_notice_names_no_single_run(
        db, business_user, starter_user):
    await db.execute(text("UPDATE users SET plan = 'business', records_limit = 5000 WHERE id = :u"),
                     {"u": starter_user.id})
    await db.commit()
    a_cfg, b_cfg = await _config(db, business_user), await _config(db, starter_user)
    a1, a2 = await _job(db, business_user, a_cfg), await _job(db, business_user, a_cfg)
    b1 = await _job(db, starter_user, b_cfg)
    await _held_row(db, business_user, a1)
    await _held_row(db, business_user, a2, property_address="2 OTHER RUN ST")
    await _held_row(db, starter_user, b1)

    late.deliver_completed_held_rows()

    [a] = await _notices(db, business_user)
    [b] = await _notices(db, starter_user)
    assert a["detail"]["count"] == 2 and sorted(a["detail"]["job_ids"]) == sorted([a1, a2]) and a["job_id"] is None
    assert b["detail"] == {"count": 1, "job_ids": [b1]} and b["job_id"] == b1


@pytest.mark.asyncio
async def test_two_notify_passes_at_once_announce_once(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    for i in range(4):
        await _held_row(db, business_user, job, property_address=f"{i} RACE ST")
    with system_sync_session() as s:   # deliver without notifying, so both passes see pending rows
        rows = s.execute(text(late._CANDIDATES_SQL), {"batch": 10, "per_account": 10}).mappings().all()
        s.rollback()
    from src.db.session import rls_sync_session

    with rls_sync_session(business_user.id) as s:
        assert all(late._deliver_one(s, r, build_snapshot([])) == "delivered" for r in rows)
    gate, out = threading.Barrier(2), []

    def run():
        gate.wait()
        out.append(late.notify_late_deliveries())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert sum(out) == 1
    [notice] = await _notices(db, business_user)
    assert notice["detail"]["count"] == 4 and await _pending(db, business_user) == 0


@pytest.mark.asyncio
async def test_a_failed_notice_leaves_the_rows_pending(db, business_user, monkeypatch):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _held_row(db, business_user, job)

    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(notification_emit, "add_notification", boom)
    stats = late.deliver_completed_held_rows()
    assert stats["delivered"] == 1 and stats["notified_accounts"] == 0
    assert await _pending(db, business_user) == 1 and await _notices(db, business_user) == []
    monkeypatch.undo()
    from src.config import settings

    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)
    late.deliver_completed_held_rows()
    assert len(await _notices(db, business_user)) == 1 and await _pending(db, business_user) == 0


def test_create_notification_keeps_its_contract(monkeypatch):
    """create_notification is now a wrapper over add_notification: unknown types still skip."""
    calls = []
    monkeypatch.setattr(notification_emit, "add_notification", lambda *a, **k: calls.append(k) or False)
    notification_emit.create_notification(user_id="x", type="not_a_type")
    assert calls == []
    notification_emit.create_notification(user_id="00000000-0000-0000-0000-000000000000", type="job_completed")
    assert calls and calls[0]["type"] == "job_completed"
