"""scripts/repair_king_property_situs.py never reads, reports or writes a privacy-withheld
lead (Phase 6 P7).

Real database; the script's own statements (_CANDIDATES_SQL, and _UPDATE_SQL through
apply()) run against it. Two King rows on one done job and one parcel, no property
address: one is withheld. The GIS answer is passed to plan() directly, so no county
service is called. Synthetic people only.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from src.db.session import system_sync_session
from tests.test_king_condo_unit_situs import _gis_row
from tests.test_privacy_enforce import _rule_row
from tests.test_repair_king_property_situs import SNAPS, _job, _row, rk

PIN = "0126039387"
GIS = {PIN: _gis_row("302 NW 203RD ST", "SHORELINE", "98177")}


async def _withhold(db, rid: str) -> None:
    rule = await _rule_row(db)
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": rid})
    await db.commit()


async def _two(db, user) -> tuple[str, str, str]:
    job_id = await _job(db, user)
    withheld = _row(db, user, job_id, PIN, fp="fp-privacy-withheld")
    kept = _row(db, user, job_id, PIN, fp="fp-privacy-kept")
    await db.commit()
    return job_id, withheld, kept


def _select(job_id: str) -> list:
    with system_sync_session() as s:
        rows = s.execute(text(rk._CANDIDATES_SQL),
                         {"record_type": "pre_foreclosure", "job_id": job_id}).all()
        s.rollback()
    return rows


def _apply(decisions) -> dict:
    with system_sync_session() as s:
        return dict(rk.apply(s, decisions, SNAPS))


async def test_a_withheld_row_is_never_a_candidate(db, business_user):
    job_id, withheld, kept = await _two(db, business_user)
    await _withhold(db, withheld)

    ids = {str(r.id) for r in await asyncio.to_thread(_select, job_id)}
    assert ids == {kept}                     # control: the deliverable twin is selected


async def test_a_row_withheld_after_the_read_is_never_written(db, business_user):
    job_id, withheld, kept = await _two(db, business_user)
    rows = await asyncio.to_thread(_select, job_id)
    decisions = rk.plan(rows, {}, GIS, {})
    assert {(str(d.row.id), d.outcome) for d in decisions} == {
        (withheld, "fill_gis"), (kept, "fill_gis")}
    await _withhold(db, withheld)

    counts = await asyncio.to_thread(_apply, decisions)

    assert counts == {"written": 1, "skipped_by_write_guard": 1}
    got = dict((await db.execute(text(
        "SELECT id::text, property_address FROM results WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [withheld, kept]})).all())
    assert got[withheld] is None
    assert got[kept] == "302 NW 203RD ST, SHORELINE, WA 98177"
