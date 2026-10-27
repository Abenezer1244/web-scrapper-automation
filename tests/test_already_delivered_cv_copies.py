"""An already-delivered code violation stays on the Already Delivered tab.

Every code violation is stamped with the qualification policy at insert, and duplicate
copies are never enriched, so a copy has no owner of its own. While the actionable rule
judged copies by the qualification gate, every prior-run copy of a code violation
vanished from the Already Delivered list and its count, although the run breakdown still
said "N already delivered". Copies repeat a lead an earlier run delivered: the views
that SHOW them do not judge them again (shown_condition). Everything that delivers,
bills, transfers a claim or buys a lookup keeps the strict rule.

Real Postgres, real endpoints, no mocks.
"""
import uuid

import pytest
from sqlalchemy import func, select, text

from src.api.download_tokens import mint_download_token
from src.api.lead_actionability import (
    actionable_condition,
    actionable_sql,
    is_actionable,
    shown_condition,
    withheld_condition,
)
from src.db.models import Result
from tests.test_lead_qualification import seattle
from tests.test_lead_qualification_worker import AFTER_RULE, _config, _job, _row


async def _copy_of_a_delivered_lead(db, user, *, reason="prior_run"):
    cfg = await _config(db, user)
    first = await _job(db, user, cfg, status="done", billed_at=AFTER_RULE,
                       export_key="exports/a.csv", record_count=1, billed_count=1)
    second = await _job(db, user, cfg, status="done", billed_at=AFTER_RULE,
                        export_key="exports/b.csv", record_count=0, billed_count=0)
    key = uuid.uuid4().hex
    await _row(db, first, user.id, seattle(party_name="DELIVERED OWNER"), dedup_hash=key)
    # The copy as dedup writes it: stamped, never enriched, so no owner of its own.
    copy = await _row(db, second, user.id, seattle(party_name=None), dedup_hash=key,
                      is_duplicate=True, duplicate_reason=reason,
                      duplicate_source_job_id=first)
    return second, copy


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["prior_run", None])
async def test_an_unenriched_copy_is_listed_and_counted(
        client, db, business_user, business_token, reason):
    """NULL reason: a row classified before migration 089 is a prior-run copy too."""
    job, copy = await _copy_of_a_delivered_lead(db, business_user, reason=reason)
    resp = await client.get(f"/jobs/{job}/results", params={"category": "already_delivered"},
                            headers={"Authorization": f"Bearer {business_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["items"]] == [copy]
    assert body["already_delivered_count"] == 1
    assert body["breakdown"]["already_delivered"] == 1
    # And the already-delivered file holds the row its tab lists.
    token = mint_download_token(str(business_user.id), job, ttl_seconds=60)
    dl = await client.get(f"/jobs/{job}/download",
                          params={"token": token, "category": "already_delivered"})
    assert dl.status_code == 200, dl.text
    assert "4512 S HOLLY ST" in dl.text


@pytest.mark.asyncio
async def test_only_the_shown_rule_exempts_a_copy(db, business_user):
    """A copy is SHOWN, never actionable: the three actionable spellings still refuse
    it (so claim transfer cannot hand it the claim), and the shown rule exempts only a
    prior-run copy, never the same unqualified row as a new lead, a same-run sibling,
    a superseded row or a row with an empty reason."""
    job, copy = await _copy_of_a_delivered_lead(db, business_user)
    cfg_rows = {"copy": copy}
    for label, kw in (("new", {}), ("same_run", {"is_duplicate": True, "duplicate_reason": "same_run"}),
                      ("superseded", {"is_duplicate": True, "duplicate_reason": "superseded"}),
                      # COALESCE maps only NULL to prior_run; an empty reason is not a copy.
                      ("empty_reason", {"is_duplicate": True, "duplicate_reason": ""})):
        cfg_rows[label] = await _row(db, job, business_user.id, seattle(party_name=None), **kw)

    for label, rid in cfg_rows.items():
        orm = (await db.execute(select(func.count()).where(
            Result.id == rid, actionable_condition()))).scalar_one() == 1
        raw = (await db.execute(text(
            f"SELECT {actionable_sql('r')} FROM results r WHERE r.id = :i"), {"i": rid})).scalar_one()
        row = (await db.execute(select(Result).where(Result.id == rid))).scalar_one()
        assert orm is raw is is_actionable(row) is False, label
        shown = (await db.execute(select(func.count()).where(
            Result.id == rid, shown_condition()))).scalar_one() == 1
        assert shown == (label == "copy"), label


@pytest.mark.asyncio
async def test_no_paid_lookup_is_bought_for_an_unqualified_copy(db, business_user):
    _job_id, copy = await _copy_of_a_delivered_lead(db, business_user)
    withheld = (await db.execute(select(func.count()).where(
        Result.id == copy, withheld_condition()))).scalar_one()
    assert withheld == 1
