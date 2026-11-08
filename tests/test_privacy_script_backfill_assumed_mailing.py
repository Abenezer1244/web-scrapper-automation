"""scripts/backfill_assumed_mailing.py never reads or writes a privacy-withheld lead.

Real database; the script's own statements run inside a transaction that is rolled
back. Two Snohomish pre-foreclosure leads whose mailing is the situs copy: one is
withheld by a privacy rule. The rule pass (_CANDIDATES, _UPDATE, _STAMP) and the
--repair-flags pass (_REPAIR, _REPAIR_UPDATE) must each select only the deliverable
row and leave the withheld row unwritten. Synthetic people only.
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.db.session import system_sync_session
from tests.test_privacy_enforce import _rule_row

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_assumed_mailing.py"
_spec = importlib.util.spec_from_file_location("_privacy_backfill_assumed_mailing", _SCRIPT)
bam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bam)

_SITUS = "1 TEST ST"
_FLAGS = {"property_state": "WA", "owner_state": None, "absentee_owner": None,
          "out_of_state_owner": None}


async def _pair(db, user, enrichment: dict) -> tuple[str, str]:
    """(withheld, kept): two Snohomish leads whose mailing copies the situs."""
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Privacy assumed mailing",
        county="snohomish", state="WA", record_type="pre_foreclosure", fields=[],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"emails": []})
    db.add(cfg)
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done",
               trigger="manual", record_count=0, billed_count=0, retry_count=0,
               created_at=datetime.now(UTC)))
    ids = []
    for _ in range(2):
        rid = str(uuid.uuid4())
        db.add(Result(id=rid, job_id=job_id, user_id=user.id, party_name="SMITH JOHN",
                      parcel_id="00000000000000", property_address=_SITUS,
                      mailing_address=f"{_SITUS}, EVERETT WA", dedup_hash=uuid.uuid4().hex,
                      is_duplicate=False, enrichment_data=dict(enrichment)))
        ids.append(rid)
    await db.commit()
    rule = await _rule_row(db)
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": ids[0]})
    await db.commit()
    return ids[0], ids[1]


def _common(rid: str) -> dict:
    return {"id": rid, "old": f"{_SITUS}, EVERETT WA", "old_property": _SITUS,
            "status": "cleared_no_source", "attempts": 1, "source": "none_no_source",
            "error": None}


async def test_rule_pass_skips_a_withheld_lead(db, business_user):
    withheld, kept = await _pair(db, business_user, {})
    with system_sync_session() as s:
        selected = {str(r.id) for r in s.execute(bam._CANDIDATES, {
            "county": "snohomish", "types": ["pre_foreclosure"],
            "terminal": list(bam._TERMINAL)})}
        stamped = {rid: s.execute(bam._STAMP, _common(rid)).rowcount
                   for rid in (withheld, kept)}
        s.rollback()
        updated = {rid: s.execute(bam._UPDATE, {**_common(rid), "mailing": None, **_FLAGS}).rowcount
                   for rid in (withheld, kept)}
        s.rollback()
    assert withheld not in selected
    assert kept in selected                     # control: the deliverable twin is selected
    assert stamped == {withheld: 0, kept: 1}
    assert updated == {withheld: 0, kept: 1}


async def test_repair_flags_pass_skips_a_withheld_lead(db, business_user):
    withheld, kept = await _pair(db, business_user, {"mailing_source": "none_no_source"})
    with system_sync_session() as s:
        selected = {str(r.id) for r in s.execute(bam._REPAIR, {"counties": ["snohomish"]})}
        written = {rid: s.execute(bam._REPAIR_UPDATE, {
            "id": rid, "old_property": _SITUS, "old": f"{_SITUS}, EVERETT WA", **_FLAGS}).rowcount
            for rid in (withheld, kept)}
        s.rollback()
    assert withheld not in selected
    assert kept in selected
    assert written == {withheld: 0, kept: 1}
