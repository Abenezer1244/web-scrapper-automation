"""scripts/backfill_king_code_violation_owner.py never reads or writes a privacy-withheld lead.

Real database; the script's own statements run inside a transaction that is rolled
back. Two otherwise identical King code-violation leads in one done job: one is
withheld by a privacy rule. Both passes (label repair and --address-points) must
select only the deliverable row, and their guarded UPDATEs must leave the withheld
row untouched. Synthetic people only.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.db.session import system_sync_session
from tests.test_privacy_enforce import _rule_row

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_king_code_violation_owner.py"
_spec = importlib.util.spec_from_file_location("_privacy_backfill_king_cv_owner", _SCRIPT)
bko = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bko)


async def _pair(db, user, ed: dict) -> tuple[str, str]:
    """(withheld, kept): two King code-violation leads in one done job."""
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Privacy CV owner", county="king",
        state="WA", record_type="code_violation", fields=[], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"emails": []})
    db.add(cfg)
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done",
               trigger="manual", record_count=0, billed_count=0, retry_count=0,
               created_at=datetime.now(UTC)))
    ids = []
    for _ in range(2):
        rid = str(uuid.uuid4())
        db.add(Result(id=rid, job_id=job_id, user_id=user.id, party_name=None, parcel_id=None,
                      property_address="1 TEST ST", property_zip="98101",
                      legal_description="000001-26CP", dedup_hash=uuid.uuid4().hex,
                      is_duplicate=False, enrichment_data=dict(ed)))
        ids.append(rid)
    await db.commit()
    rule = await _rule_row(db)
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": ids[0]})
    await db.commit()
    return ids[0], ids[1]


async def test_label_repair_skips_a_withheld_lead(db, business_user):
    withheld, kept = await _pair(db, business_user, {"source": bko._SOURCE})
    with system_sync_session() as s:
        selected = {str(r.id) for r in s.execute(
            text(bko._CANDIDATES_SQL), {"source": bko._SOURCE, "retry_owners": False})}
        written = {}
        for rid in (withheld, kept):
            written[rid] = s.execute(text(bko._UPDATE_SQL), {
                "new_party": "OWNER TEST", "old_party": None, "rid": rid,
                "uid": business_user.id, "payload": json.dumps({"cv_semantics_repaired_at": "x"}),
                "source": bko._SOURCE, "old_pin": None, "old_pin_status": None,
                "old_pin_match": None, "old_pin_source": None}).rowcount
        s.rollback()
    assert withheld not in selected
    assert kept in selected                     # control: the deliverable twin is selected
    assert written == {withheld: 0, kept: 1}


async def test_address_points_pass_skips_a_withheld_lead(db, business_user):
    ed = {"source": bko._SOURCE, "kc_pin_status": "no_parcel",
          "latitude": "47.6", "longitude": "-122.3"}
    withheld, kept = await _pair(db, business_user, ed)
    with system_sync_session() as s:
        selected = {str(r.id) for r in s.execute(text(bko._AP_CANDIDATES_SQL), {
            "source": bko._SOURCE, "statuses": ["no_parcel"]})}
        written = {}
        for rid in (withheld, kept):
            written[rid] = s.execute(text(bko._AP_UPDATE_SQL), {
                "new_mail": "PO BOX 1, SEATTLE, WA 98101", "old_mail": None, "rid": rid,
                "uid": business_user.id, "payload": json.dumps({"kc_address_point_checked": "x"}),
                "source": bko._SOURCE, "f_property_state": "WA", "f_owner_state": "WA",
                "f_absentee": True, "f_out_of_state": False,
                "old_pin_status": "no_parcel", "old_pin": None, "old_pin_match": None,
                "old_pin_source": None, "old_parcel_address": None,
                "old_address": "1 TEST ST", "old_zip": "98101", "old_lat": "47.6",
                "old_lon": "-122.3", "old_city": None, "old_state": None,
                "old_ed": json.dumps(ed), "old_owner_state": None, "old_absentee": None,
                "old_out_of_state": None}).rowcount
        s.rollback()
    assert withheld not in selected
    assert kept in selected
    assert written == {withheld: 0, kept: 1}
