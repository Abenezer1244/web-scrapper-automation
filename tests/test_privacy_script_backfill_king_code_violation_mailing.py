"""scripts/backfill_king_code_violation_mailing.py never reads or writes a privacy-withheld lead.

Real database; the script's own statements run inside a transaction that is rolled
back. Two otherwise identical King code-violation leads with coordinates and no
mailing, in one done job: one is withheld by a privacy rule. The candidate query must
select only the deliverable row and the guarded UPDATE must leave the withheld row
untouched. Synthetic people only.
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

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_king_code_violation_mailing.py"
_spec = importlib.util.spec_from_file_location("_privacy_backfill_king_cv_mailing", _SCRIPT)
bkm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bkm)


async def test_a_withheld_lead_is_neither_selected_nor_written(db, business_user):
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="Privacy CV mailing",
        county="king", state="WA", record_type="code_violation", fields=[], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"emails": []})
    db.add(cfg)
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=business_user.id, scraper_config_id=cfg.id, status="done",
               trigger="manual", record_count=0, billed_count=0, retry_count=0,
               created_at=datetime.now(UTC)))
    withheld, kept = str(uuid.uuid4()), str(uuid.uuid4())
    for rid in (withheld, kept):
        db.add(Result(id=rid, job_id=job_id, user_id=business_user.id, party_name=None,
                      parcel_id=None, mailing_address=None, property_address="1 TEST ST",
                      dedup_hash=uuid.uuid4().hex, is_duplicate=False,
                      enrichment_data={"latitude": "47.6", "longitude": "-122.3"}))
    await db.commit()
    rule = await _rule_row(db)
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": withheld})
    await db.commit()

    with system_sync_session() as s:
        selected = {str(r.id) for r in s.execute(text(bkm._CANDIDATES_SQL))}
        written = {rid: s.execute(text(bkm._UPDATE_SQL), {
            "mail": "PO BOX 1, SEATTLE, WA 98101", "rid": rid, "uid": business_user.id,
            "payload": json.dumps({"kc_pin_status": "matched"}),
            "f_property_state": "WA", "f_owner_state": "WA", "f_absentee": True,
            "f_out_of_state": False}).rowcount for rid in (withheld, kept)}
        s.rollback()
    assert withheld not in selected
    assert kept in selected                     # control: the deliverable twin is selected
    assert written == {withheld: 0, kept: 1}
