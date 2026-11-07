"""The orphaned-duplicate repair never lets a privacy-withheld row claim a lead
(Phase 6 P5g, read inventory gap 8c).

Real database; the script's own claim statement (_Q_CLAIM) runs inside a transaction
that is rolled back. Two orphaned duplicates of one lead in one account: the OLDER one
is withheld, so without the actionable predicate it would win the claim. Synthetic
people only.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from sqlalchemy import text

from src.db.session import system_sync_session
from tests.test_privacy_enforce import _job, _rows, _rule_row


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "repair_orphaned_duplicate_flags.py"
    spec = importlib.util.spec_from_file_location("_repair_orphans", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_a_withheld_orphan_never_wins_the_claim(db, business_user):
    rule = await _rule_row(db)
    older_job = await _job(db, business_user)
    newer_job = await _job(db, business_user)
    (withheld,) = await _rows(db, business_user, older_job, ("SMITH JOHN", "1234567890"))
    (kept,) = await _rows(db, business_user, newer_job, ("SMITH JOHN", "1234567890"))
    lead_hash = uuid.uuid4().hex
    await db.execute(text(
        "UPDATE jobs SET created_at = now() - interval '2 days' WHERE id = :j"), {"j": older_job})
    await db.execute(text(
        "UPDATE results SET is_duplicate = true, duplicate_reason = 'prior_run', "
        "dedup_hash = :h, property_address = '1 TEST ST', mailing_address = 'PO BOX 1' "
        "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"h": lead_hash, "ids": [withheld, kept]})
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :r "
        "WHERE id = :i"), {"r": rule, "i": withheld})
    await db.commit()
    with system_sync_session() as s:
        claimed = [str(x) for x in s.execute(text(_script()._Q_CLAIM),
                                             {"uid": business_user.id, "jid": None}).scalars()]
        s.rollback()
    assert withheld not in claimed
    assert claimed == [kept]          # control: the deliverable twin does take the claim
