"""scripts/backfill_property_situs_parts.py never reads, reports or writes a privacy-withheld
lead, and never takes situs evidence from a privacy-withheld notice (Phase 6 P7).

Real database; the script's own statements (_CANDIDATES, _UPDATE) run inside a sync
session that is rolled back. Synthetic people only.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from src.db.models import NtsNotice
from src.db.session import system_sync_session
from tests.test_privacy_enforce import _job, _rows, _rule_row

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_property_situs_parts.py"
_spec = importlib.util.spec_from_file_location("_privacy_backfill_situs_parts", _SCRIPT)
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


async def _withhold(db, rule: str, rid: str) -> None:
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": rid})
    await db.commit()


def _candidates(ids: list[str]) -> dict[str, object]:
    with system_sync_session() as s:
        rows = {str(r.id): r for r in s.execute(bf._CANDIDATES).fetchall() if str(r.id) in ids}
        s.rollback()
    return rows


def _update(rid: str) -> int:
    with system_sync_session() as s:
        rc = s.execute(bf._UPDATE, {
            "id": rid, "city": "SPANAWAY", "state": "WA", "zip": "98387",
            "owner_state": None, "absentee_owner": None, "out_of_state_owner": None,
        }).rowcount
        s.rollback()
    return rc


async def test_a_withheld_lead_is_never_selected_or_written(db, business_user):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    withheld, kept = await _rows(db, business_user, job, ("SMITH JOHN", "1234567890"),
                                 ("DOE JANE", "5555555555"))
    await _withhold(db, rule, withheld)

    got = await asyncio.to_thread(_candidates, [withheld, kept])
    assert withheld not in got
    assert kept in got                       # control: the deliverable twin is a candidate
    assert await asyncio.to_thread(_update, withheld) == 0
    assert await asyncio.to_thread(_update, kept) == 1


async def test_a_withheld_notice_gives_no_situs_evidence(db, business_user):
    rule = await _rule_row(db)
    job = await _job(db, business_user)
    on_withheld, on_clean = await _rows(db, business_user, job, ("DOE JANE", "5555555555"),
                                        ("ROE RICHARD", "6666666666"))
    tag = uuid.uuid4().hex[:8]
    withheld_notice = NtsNotice(id=str(uuid.uuid4()), source="tacoma_daily_index",
                                ts_number=f"TEST-PRIV-{tag}-W", county="king", state="WA",
                                property_address="1 TEST ST, SPANAWAY, WA 98387", grantor=None,
                                is_active=False, privacy_suppression_id=rule,
                                fetched_at=datetime.now(UTC))
    clean_notice = NtsNotice(id=str(uuid.uuid4()), source="tacoma_daily_index",
                             ts_number=f"TEST-PRIV-{tag}-C", county="king", state="WA",
                             property_address="1 TEST ST, SPANAWAY, WA 98387",
                             is_active=True, fetched_at=datetime.now(UTC))
    db.add_all([withheld_notice, clean_notice])
    await db.commit()
    try:
        await db.execute(text("UPDATE results SET nts_notice_id = :n WHERE id = :i"),
                         {"n": withheld_notice.id, "i": on_withheld})
        await db.execute(text("UPDATE results SET nts_notice_id = :n WHERE id = :i"),
                         {"n": clean_notice.id, "i": on_clean})
        await db.commit()

        got = await asyncio.to_thread(_candidates, [on_withheld, on_clean])
        assert got[on_withheld].notice_situs is None    # the lead stays, the notice does not
        assert got[on_clean].notice_situs == "1 TEST ST, SPANAWAY, WA 98387"   # control
    finally:
        await db.execute(text("UPDATE results SET nts_notice_id = NULL "
                              "WHERE id = ANY(CAST(:ids AS uuid[]))"),
                         {"ids": [on_withheld, on_clean]})
        await db.execute(text("DELETE FROM nts_notices WHERE id = ANY(CAST(:ids AS uuid[]))"),
                         {"ids": [withheld_notice.id, clean_notice.id]})
        await db.commit()
