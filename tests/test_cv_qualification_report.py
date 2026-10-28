"""The historical dry run (scripts/cv_qualification_report.py): real rows, real read-only transaction.

The owner decides quota credit for code violations billed before the gate from this report,
so it must count each bucket correctly, judge a pre-gate row as if stamped without writing
the stamp, never print PII, and be unable to write at all.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from scripts import cv_qualification_report as rep
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.api.lead_actionability import NOT_QUALIFIED, OVER_QUOTA
from src.db.session import system_sync_session
from src.workers.qualification_quality import GATE_DEPLOYED_AT
from tests.test_lead_qualification import seattle
from tests.test_qualification_quality import _config, _job, _row

PRIVATE = ("HARTLEY MARGARET A", "4512 S HOLLY ST", "1047891-CF")


def _legacy(**kw) -> dict:
    """A Seattle case from before the gate: no policy stamp."""
    spec = seattle(**kw)
    spec["enrichment_data"] = {k: v for k, v in spec["enrichment_data"].items() if k != "qualification_policy"}
    return spec


def _report() -> dict:
    with system_sync_session() as s:
        return rep.build_report(s)


@pytest.mark.asyncio
async def test_every_bucket_is_counted_and_pre_gate_rows_are_judged_as_if_stamped(db, business_user):
    cfg = await _config(db, business_user)
    uid = business_user.id
    pre = await _job(db, business_user, cfg, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    await db.execute(text("UPDATE jobs SET billed_count = 4 WHERE id = :j"), {"j": pre})
    await _row(db, pre, uid, _legacy())                                              # pass
    await _row(db, pre, uid, _legacy(party_name=None, ed={"owner_source": None}))    # fail: owner
    await _row(db, pre, uid, _legacy(party_name=None, mailing_address=None,
                                     ed={"owner_source": None}))                     # fail: owner + mailing
    await _row(db, pre, uid, _legacy(ed={"cv_owner_recovery_outcome": "found"}))     # pass, named later
    await _row(db, pre, uid, _legacy(), mark=OVER_QUOTA)                             # not a candidate
    await _row(db, pre, uid, _legacy(property_address=None, mailing_address=None))   # no address
    await _row(db, pre, uid, _legacy(), is_duplicate=True, duplicate_reason="prior_run",
               duplicate_source_job_id=str(uuid.uuid4()))                            # source gone
    post = await _job(db, business_user, cfg, hours_ago=2)
    await _row(db, post, uid, seattle())
    await _row(db, post, uid, seattle(party_name=None), mark=NOT_QUALIFIED)
    await _row(db, post, uid, seattle(), mark=NOT_QUALIFIED)                         # complete now
    await db.commit()

    report = _report()
    runs = {r["job"]: r for r in report["runs"]}
    a, b = runs[pre[:8]], runs[post[:8]]
    assert (a["gate"], a["billed"], a["candidates"], a["strict_pass"], a["strict_fail"]) == ("pre", 4, 4, 2, 2)
    assert a["reasons"] == {"missing_owner": 2, "mailing_unresolved": 1}
    assert a["named_after_delivery"] == 1
    assert (a["prior_run_copies"], a["copies_source_gone"]) == (1, 1)
    assert a["by_jurisdiction"] == {"seattle_sdci_code_violations": {"candidates": 4, "strict_fail": 2}}
    assert (b["gate"], b["candidates"], b["held"], b["held_complete_now"]) == ("post", 1, 2, 1)
    [account] = report["accounts"].values()
    assert (account["billed_pre_gate"], account["strict_fail_pre_gate"], account["held_post_gate"]) == (4, 2, 2)

    # Judged as if stamped, never written: the legacy rows still carry no stamp.
    stamped = (await db.execute(text(
        "SELECT count(*) FROM results WHERE job_id = :j AND enrichment_data->>'qualification_policy' IS NOT NULL"),
        {"j": pre})).scalar_one()
    assert stamped == 0


@pytest.mark.asyncio
async def test_the_output_carries_no_owner_address_or_case_number(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    await _row(db, job, business_user.id, _legacy(party_name=None, ed={"owner_source": None}))
    report = _report()
    import json

    for out in (rep.render(report), json.dumps(report, default=str)):
        assert not any(value in out for value in PRIVATE)
        assert business_user.id not in out  # only the short prefix


def test_the_transaction_refuses_writes():
    with system_sync_session() as s:
        rep.build_report(s)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            s.execute(text("UPDATE jobs SET billed_count = billed_count WHERE false"))
        s.rollback()


def test_account_prefixes_lengthen_when_two_collide():
    a = "abcdef01" + "1" * 28
    b = "abcdef01" + "2" * 28
    names = rep.short_ids({a, b, "99999999" + "0" * 28})
    assert len(set(names.values())) == 3
    assert names[a] != names[b] and len(names[a]) == 12
