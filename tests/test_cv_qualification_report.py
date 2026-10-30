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


# ── Codex review r1 (P2 overlap, Tacoma, reconciliation; P3 coverage) ─────────

def _tacoma(**kw) -> dict:
    """A Tacoma case on its printed Pierce parcel, named by ATIP; from before the gate."""
    ed = {"source": "tacoma_code_violations", "case_number": "CODE-2026-0042",
          "case_type": "Nuisance", "owner_source": "pierce_atip", "owner_pin": "2012345678"}
    ed.update(kw.pop("ed", {}))
    row = {"party_name": "TAHOMA PROPERTIES LLC", "parcel_id": "2012345678",
           "property_address": "5110 S PINE ST", "mailing_address": "PO BOX 98, TACOMA, WA 98401",
           "date_recorded": "09/02/2026", "enrichment_data": ed}
    row.update(kw)
    return row


@pytest.mark.asyncio
async def test_named_later_and_still_failing_rows_are_counted_once(db, business_user):
    cfg = await _config(db, business_user)
    uid = business_user.id
    job = await _job(db, business_user, cfg, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    await db.execute(text("UPDATE jobs SET billed_count = 5 WHERE id = :j"), {"j": job})
    await _row(db, job, uid, _legacy(mailing_address=None, ed={"cv_owner_recovery_outcome": "found"}))  # both
    await _row(db, job, uid, _legacy(party_name=None, ed={"owner_source": None}))                        # fail only
    await _row(db, job, uid, _legacy(ed={"cv_owner_recovery_outcome": "found"}))                         # named only
    await _row(db, job, uid, _tacoma(ed={"owner_recovery_last_at": "2026-09-20T10:00:00Z"}))             # Tacoma named
    await _row(db, job, uid, _tacoma())                                                                  # clean
    await db.commit()
    r = {x["job"]: x for x in _report()["runs"]}[job[:8]]
    assert (r["candidates"], r["strict_fail"], r["named_after_delivery"]) == (5, 2, 3)
    assert (r["named_and_still_failing"], r["affected"]) == (1, 4)
    assert r["billed_minus_candidates"] == 0
    assert r["by_jurisdiction"]["tacoma_code_violations"] == {"candidates": 2, "strict_fail": 0}


@pytest.mark.asyncio
async def test_billed_and_candidates_that_disagree_are_reported_not_hidden(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    await db.execute(text("UPDATE jobs SET billed_count = 7 WHERE id = :j"), {"j": job})
    await _row(db, job, business_user.id, _legacy())
    report = _report()
    r = {x["job"]: x for x in report["runs"]}[job[:8]]
    assert r["billed_minus_candidates"] == 6
    [a] = report["accounts"].values()
    assert a["unreconciled_runs_pre_gate"] == 1
    assert "evidence, not an exact count" in rep.render(report)


@pytest.mark.asyncio
async def test_a_copy_whose_source_is_another_accounts_run_counts_as_gone(db, business_user, starter_user):
    mine, theirs = await _config(db, business_user), await _config(db, starter_user)
    old_mine = await _job(db, business_user, mine, started=GATE_DEPLOYED_AT - timedelta(days=9), hours_ago=200)
    old_theirs = await _job(db, starter_user, theirs, started=GATE_DEPLOYED_AT - timedelta(days=9), hours_ago=200)
    job = await _job(db, business_user, mine, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    copy = {"is_duplicate": True, "duplicate_reason": "prior_run"}
    await _row(db, job, business_user.id, _legacy(), duplicate_source_job_id=old_mine, **copy)
    await _row(db, job, business_user.id, _legacy(), duplicate_source_job_id=old_theirs, **copy)
    await _row(db, job, business_user.id, _legacy(), duplicate_source_job_id=None, **copy)
    report = _report()
    r = {x["job"]: x for x in report["runs"]}[job[:8]]
    assert (r["prior_run_copies"], r["copies_source_gone"]) == (3, 2)
    assert len(report["accounts"]) == 2  # two tenants, never merged


@pytest.mark.asyncio
async def test_the_report_writes_nothing_anywhere(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    await _row(db, job, business_user.id, _legacy(party_name=None, ed={"owner_source": None}))
    q = text("SELECT (SELECT count(*) FROM audit_events), (SELECT md5(string_agg(r::text, '' ORDER BY id)) "
             "FROM results r WHERE job_id = :j), (SELECT md5(j::text) FROM jobs j WHERE id = :j)")
    with system_sync_session() as s:
        before = s.execute(q, {"j": job}).one()
    _report()
    with system_sync_session() as s:
        assert s.execute(q, {"j": job}).one() == before


def test_a_failed_run_prints_the_exception_class_only(monkeypatch, capsys):
    def boom(db):
        raise RuntimeError("party_name=HARTLEY MARGARET A at 4512 S HOLLY ST")

    monkeypatch.setattr(rep, "build_report", boom)
    monkeypatch.setattr("sys.argv", ["cv_qualification_report.py"])
    assert rep.main() == 1
    out = capsys.readouterr()
    assert out.err.strip() == "Report failed: RuntimeError" and out.out == ""


@pytest.mark.asyncio
async def test_more_candidates_than_billed_is_unreconciled_too(db, business_user):
    """Codex r2 P3: an address filled after billing adds a candidate the run never billed."""
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg, started=GATE_DEPLOYED_AT - timedelta(days=3), hours_ago=60)
    await db.execute(text("UPDATE jobs SET billed_count = 1 WHERE id = :j"), {"j": job})
    await _row(db, job, business_user.id, _legacy())
    await _row(db, job, business_user.id, _legacy())
    report = _report()
    assert {x["job"]: x for x in report["runs"]}[job[:8]]["billed_minus_candidates"] == -1
    [a] = report["accounts"].values()
    assert a["unreconciled_runs_pre_gate"] == 1


def test_the_documented_command_imports_the_app():
    """Codex r2 P2: `python scripts/cv_qualification_report.py` must find src/ by itself."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run([sys.executable, str(root / "scripts" / "cv_qualification_report.py"), "--help"],
                          cwd=root / "scripts", env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-400:]
    assert "--json" in done.stdout


@pytest.mark.asyncio
async def test_a_late_delivered_row_is_counted_on_its_own(db, business_user):
    """Held at the gate, delivered and charged later: never a candidate the run billed."""
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg, hours_ago=2)
    late_ed = {**seattle()["enrichment_data"],
               "late_delivery": {"delivered_at": "2026-10-30T12:00:00+00:00", "policy": "code_violation_v1"},
               "qualification_hold": {"reasons": ["missing_owner"], "policy": "code_violation_v1"}}
    await _row(db, job, business_user.id, {**seattle(), "enrichment_data": late_ed})
    await _row(db, job, business_user.id, seattle())
    r = {x["job"]: x for x in _report()["runs"]}[job[:8]]
    assert (r["late_delivered"], r["candidates"], r["held"]) == (1, 1, 0)
