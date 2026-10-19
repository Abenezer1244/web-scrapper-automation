"""src/workers/tasks_helpers/owner_flags.py: owner flags written set-based (Phase 3b).

Real DB. A ~15k-row King tax re-run flushed one ORM UPDATE per row (~6 minutes);
the helper writes chunked UPDATEs, skips any row something else changed after it
was read, and leaves the caller's ORM objects matching what was stored.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from src.db.models import Job, Result, ScraperConfig
from src.utils.address_intel import compute_owner_flags

pytestmark = pytest.mark.asyncio

_WA = "123 MAIN ST, SEATTLE, WA 98101"
_OR = "PO BOX 1, PORTLAND, OR 97201"


async def _job(db, user) -> str:
    cfg = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="flags", county="king",
                        state="WA", record_type="tax_delinquent", fields=[], enrichment=[],
                        schedule={}, deliver={})
    db.add(cfg)
    await db.flush()
    job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
              status="enriching", trigger="manual")
    db.add(job)
    await db.commit()
    return job.id


async def _row(db, user, job_id, mailing, **flags) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="X",
                  property_address=_WA, mailing_address=mailing, **flags))
    await db.commit()
    return rid


async def _flags(db, rid) -> tuple:
    r = (await db.execute(text(
        "SELECT property_state, owner_state, absentee_owner, out_of_state_owner "
        "FROM results WHERE id = :i"), {"i": rid})).one()
    return tuple(r)


def _run(job_id, user_id, other_ids=(), change_before_write=None,
         change_sql="UPDATE results SET mailing_address = '" + _WA + "' WHERE id = :i"):
    """Load the job's rows (plus `other_ids`) in one session, optionally let another
    connection change a row after the read, then recompute. Returns the helper's
    counts, each object's flags as the ORM sees them, and whether the session is dirty."""
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.owner_flags import FLAG_COLUMNS, recompute_owner_flags

    with system_sync_session() as sdb:
        rows = sdb.execute(select(Result).where(
            (Result.job_id == job_id) | Result.id.in_(list(other_ids)))).scalars().all()
        for r in rows:  # read every input now, as the job's refetch does
            _ = (r.property_address, r.mailing_address, r.owner_state)
        if change_before_write:
            with system_sync_session() as other:
                other.execute(text(change_sql), {"i": change_before_write})
                other.commit()
        counts = recompute_owner_flags(sdb, rows, job_id, user_id)
        seen = {str(r.id): tuple(getattr(r, c) for c in FLAG_COLUMNS) for r in rows}
        return counts, seen, bool(sdb.dirty)


async def test_changed_rows_are_written_and_the_objects_match(db, starter_user):
    job_id = await _job(db, starter_user)
    stale = await _row(db, starter_user, job_id, _OR)
    # Already holds exactly what it would be recomputed to: not written at all.
    current = await _row(db, starter_user, job_id, _WA, **compute_owner_flags(_WA, _WA))
    before = await _flags(db, current)

    (written, skipped), seen, dirty = await asyncio.to_thread(_run, job_id, starter_user.id)

    db.expire_all()
    assert (written, skipped) == (1, 0)
    got = await _flags(db, stale)
    assert got[1] == "OR" and got[3] is True   # owner_state, out_of_state_owner
    assert seen[stale] == got                    # the object shows what is stored
    assert seen[current] == await _flags(db, current)
    assert not dirty
    assert await _flags(db, current) == before


async def test_a_row_changed_after_it_was_read_is_left_alone(db, starter_user):
    job_id = await _job(db, starter_user)
    moved = await _row(db, starter_user, job_id, _OR)

    (written, skipped), seen, _ = await asyncio.to_thread(
        _run, job_id, starter_user.id, (), moved)

    db.expire_all()
    assert (written, skipped) == (0, 1)
    # Not overwritten with flags computed from the mailing address read before the change.
    assert await _flags(db, moved) == (None, None, None, None)
    assert seen[moved] == (None, None, None, None)


async def test_a_flag_changed_after_it_was_read_is_kept_and_the_object_reloads(db, starter_user):
    job_id = await _job(db, starter_user)
    moved = await _row(db, starter_user, job_id, _OR)

    (written, skipped), seen, _ = await asyncio.to_thread(
        _run, job_id, starter_user.id, (), moved,
        "UPDATE results SET owner_state = 'ID' WHERE id = :i")

    db.expire_all()
    assert (written, skipped) == (0, 1)
    assert (await _flags(db, moved))[1] == "ID"
    # The caller's object shows the concurrent value, not the stale read.
    assert seen[moved][1] == "ID"


async def test_another_accounts_row_is_never_written(db, starter_user, business_user):
    job_id = await _job(db, starter_user)
    other_job = await _job(db, business_user)
    theirs = await _row(db, business_user, other_job, _OR)

    (written, skipped), _, _ = await asyncio.to_thread(
        _run, job_id, starter_user.id, (theirs,))

    db.expire_all()
    assert (written, skipped) == (0, 1)
    assert await _flags(db, theirs) == (None, None, None, None)
