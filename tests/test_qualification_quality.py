"""Monitoring the code-violation qualification gate: real Postgres rows, real alert rows.

Run 1ad28ded shipped 491 unnamed code violations and nothing paged. The gate now holds
them; these tests pin the checks that watch the gate (src/workers/qualification_quality.py):
  - the three invariants fire on a breach and stay quiet on everything the gate does on
    purpose (held rows, duplicates, over-quota rows, refused or queued outbound work,
    jobs from before the gate, jobs still running, other record types);
  - the yield is frozen at the gate, judged per jurisdiction against that jurisdiction's
    own post-gate runs, and counts over-quota rows as passes;
  - one alert per breached kind, none in report mode; the sweep runs the gate check.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, NOT_QUALIFIED, OVER_QUOTA
from src.api.lead_qualification import HOLD_KEY, POLICY_CODE_VIOLATION
from src.db.models import DialerDelivery, Job, PendingSkipTraceRow, Result, ScraperConfig
from src.db.session import system_sync_session
from src.workers import data_quality as dq
from src.workers import qualification_quality as qq
from tests.test_lead_qualification import bellevue, seattle

SEATTLE = "seattle_sdci_code_violations"
BELLEVUE = "bellevue_code_enforcement"


async def _config(db, user, record_type="code_violation") -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King code violations", county="king",
        state="WA", record_type=record_type, fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user, cfg, *, status="done", started=None, hours_ago=1.0) -> str:
    assert datetime.now(UTC) > qq.GATE_DEPLOYED_AT
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status=status,
               trigger="manual", record_count=0, billed_count=0,
               started_at=started or now - timedelta(hours=hours_ago + 0.5),
               finished_at=now - timedelta(hours=hours_ago) if status == "done" else None))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, spec: dict, *, mark: str | None = None, hold=None,
               **kw) -> str:
    rid = str(uuid.uuid4())
    fields = {k: v for k, v in spec.items() if k not in ("phone", "email")}
    ed = dict(fields.pop("enrichment_data") or {})
    if mark:
        ed[DELIVERY_EXCLUDED_KEY] = mark
    if hold is not None:
        ed[HOLD_KEY] = hold
    fields.update(kw)
    fields.setdefault("is_duplicate", False)
    fields.setdefault("dedup_hash", uuid.uuid4().hex)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, enrichment_data=ed, **fields))
    await db.commit()
    return rid


def _held(**kw) -> dict:
    """A Seattle case the gate held for a missing owner."""
    return seattle(party_name=None, ed={"owner_source": None, "owner_pin": None}, **kw)


def _measure(fn):
    with system_sync_session() as s:
        return fn(s)


def _mine(found: dict, *job_ids: str) -> dict:
    """The invariant hits for this test's jobs only (the check is global by design)."""
    return {kind: {j: n for j, n in jobs if j in job_ids} for kind, jobs in found.items()}


async def _audit(path: str) -> list[str]:
    rows: list = []
    for _ in range(30):
        with system_sync_session() as s:
            rows = s.execute(text(
                "SELECT detail FROM audit_events WHERE event = 'ops_alert' AND path = :p"),
                {"p": path}).scalars().all()
        if rows:
            break
        await asyncio.sleep(0.1)
    return rows


# ── invariants ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_unheld_failing_row_is_a_breach_and_nothing_the_gate_does_on_purpose_is(
        db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    uid = business_user.id
    await _row(db, job, uid, _held())                                     # breach
    await _row(db, job, uid, _held(property_address=None, mailing_address=None))  # breach: no-address rows are held too
    await _row(db, job, uid, _held(), mark=NOT_QUALIFIED)                 # held: fine
    await _row(db, job, uid, seattle(), mark=OVER_QUOTA)                  # capped: fine
    await _row(db, job, uid, seattle())                                   # qualified: fine
    await _row(db, job, uid, _held(), is_duplicate=True, duplicate_reason="prior_run")
    await _row(db, job, uid, _held(), is_duplicate=True, duplicate_reason="same_run")
    found = _mine(_measure(qq.gate_invariants), job)
    assert found["unheld_unqualified"] == {job: 2}
    assert found["outbound_held"] == {} and found["unstamped"] == {}


@pytest.mark.asyncio
async def test_a_delivered_lead_that_loses_its_owner_later_is_caught(db, business_user):
    """Drift after the run was judged: the check is recurring, so the next tick sees it."""
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _row(db, job, business_user.id, seattle())
    assert _mine(_measure(qq.gate_invariants), job)["unheld_unqualified"] == {}
    await db.execute(text("UPDATE results SET party_name = NULL WHERE id = :i"), {"i": rid})
    await db.commit()
    assert _mine(_measure(qq.gate_invariants), job)["unheld_unqualified"] == {job: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["pre_gate", "running", "probate", "unstamped_legacy"])
async def test_rows_outside_the_gate_are_never_judged(db, business_user, variant):
    cfg = await _config(db, business_user, record_type="probate" if variant == "probate" else "code_violation")
    if variant == "pre_gate":
        job = await _job(db, business_user, cfg, started=qq.GATE_DEPLOYED_AT - timedelta(minutes=5))
    elif variant == "running":
        job = await _job(db, business_user, cfg, status="enriching")
    else:
        job = await _job(db, business_user, cfg, started=qq.GATE_DEPLOYED_AT - timedelta(days=1)) \
            if variant == "unstamped_legacy" else await _job(db, business_user, cfg)
    spec = _held()
    if variant in ("probate", "unstamped_legacy"):
        spec["enrichment_data"] = {k: v for k, v in spec["enrichment_data"].items() if k != "qualification_policy"}
    await _row(db, job, business_user.id, spec)
    found = _mine(_measure(qq.gate_invariants), job)
    assert found == {"unheld_unqualified": {}, "outbound_held": {}, "unstamped": {}}


@pytest.mark.asyncio
async def test_a_post_gate_code_violation_without_the_stamp_is_a_breach(db, business_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    spec = seattle()
    del spec["enrichment_data"]["qualification_policy"]
    await _row(db, job, business_user.id, spec)
    await _row(db, job, business_user.id, seattle())
    found = _mine(_measure(qq.gate_invariants), job)
    assert found["unstamped"] == {job: 1}
    assert found["unheld_unqualified"] == {}  # unstamped rows pass the policy: that is the danger


@pytest.mark.asyncio
async def test_only_outbound_work_that_went_out_on_a_held_row_is_a_breach(db, business_user, starter_user):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    uid = business_user.id
    held = [await _row(db, job, uid, _held(), mark=NOT_QUALIFIED) for _ in range(6)]
    fine = await _row(db, job, uid, seattle())

    def dialer(rid, status, user_id=uid):
        db.add(DialerDelivery(id=str(uuid.uuid4()), job_id=job, result_id=rid, user_id=user_id,
                              scraper_config_id=cfg.id, vendor_id="phoneburner", status=status))

    def trace(rid, status):
        db.add(PendingSkipTraceRow(id=str(uuid.uuid4()), job_id=job, result_id=rid, user_id=uid,
                                   property_address="4512 S HOLLY ST", status=status))

    dialer(held[0], "delivered")         # breach
    trace(held[1], "submitted")          # breach
    trace(held[2], "completed")          # breach
    dialer(held[3], "skipped")           # refused: the gate working
    trace(held[4], "cancelled")          # refused
    trace(held[5], "queued")             # not spent
    dialer(fine, "delivered")            # a qualified lead: fine
    await db.commit()
    found = _mine(_measure(qq.gate_invariants), job)
    assert found["outbound_held"] == {job: 3}


@pytest.mark.asyncio
async def test_outbound_work_attributed_to_another_account_never_joins(db, business_user, starter_user):
    """dialer_deliveries.user_id must match the result's account (tenant-safe join)."""
    cfg = await _config(db, business_user)
    other_cfg = await _config(db, starter_user)
    job = await _job(db, business_user, cfg)
    rid = await _row(db, job, business_user.id, _held(), mark=NOT_QUALIFIED)
    db.add(DialerDelivery(id=str(uuid.uuid4()), job_id=job, result_id=rid, user_id=starter_user.id,
                          scraper_config_id=other_cfg.id, vendor_id="phoneburner", status="delivered"))
    await db.commit()
    assert _mine(_measure(qq.gate_invariants), job)["outbound_held"] == {}


@pytest.mark.asyncio
async def test_one_alert_per_breached_kind_and_none_in_report_mode(db, business_user, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")  # no e-mail; the durable row still lands
    cfg = await _config(db, business_user)
    jobs = [await _job(db, business_user, cfg) for _ in range(3)]
    for job in jobs:
        await _row(db, job, business_user.id, _held())
    import src.workers.ops_alerts as oa

    sent: list = []
    real = oa.send_ops_alert
    monkeypatch.setattr(oa, "send_ops_alert", lambda *a, **k: sent.append(a) or real(*a, **k))
    _measure(lambda s: qq.check_gate_invariants(s, alert=False))
    assert sent == []
    _measure(qq.check_gate_invariants)
    assert [(kind, key) for kind, key, *_ in sent] == [("qualification_gate", "unheld_unqualified")]
    details = await _audit("qualification_gate:unheld_unqualified")
    assert details and "CRITICAL" in details[0]


# ── yield ────────────────────────────────────────────────────────────────────

async def _cohort_run(db, user, cfg, *, n: int, held: int, spec=seattle, hours_ago=30.0,
                      hold=None, over_quota: int = 0) -> str:
    job = await _job(db, user, cfg, hours_ago=hours_ago)
    for i in range(n):
        if i < held:
            await _row(db, job, user.id, spec(party_name=None), mark=NOT_QUALIFIED, hold=hold)
        elif i < held + over_quota:
            await _row(db, job, user.id, spec(), mark=OVER_QUOTA)
        else:
            await _row(db, job, user.id, spec())
    return job


def _yield(job_id: str) -> dict:
    return _measure(lambda s: qq.qualification_yield(
        s, job_id=job_id, county="KING", state="wa", record_type="code_violation"))


@pytest.mark.asyncio
async def test_a_jurisdiction_whose_pass_rate_collapses_warns_with_its_frozen_reasons(db, business_user):
    cfg = await _config(db, business_user)
    await _cohort_run(db, business_user, cfg, n=60, held=6)
    run = await _cohort_run(db, business_user, cfg, n=18, held=18, hours_ago=1,
                            hold={"reasons": ["missing_owner"], "policy": POLICY_CODE_VIOLATION})
    # Two held rows from before the record existed: reasons not recorded, never "no reason".
    for _ in range(2):
        await _row(db, run, business_user.id, seattle(party_name=None), mark=NOT_QUALIFIED)
    measured = _yield(run)
    warnings, unjudged = qq.evaluate_yield(measured)
    assert unjudged == []
    [w] = warnings
    assert (w["field"], w["run_pct"], w["baseline_pct"]) == (SEATTLE, 0.0, 90.0)
    assert w["reasons"] == {"missing_owner": 18, qq.UNRECORDED: 2}


@pytest.mark.asyncio
async def test_the_yield_is_frozen_at_the_gate(db, business_user):
    """A sweep completing held rows later never moves the number: the mark decides."""
    cfg = await _config(db, business_user)
    run = await _cohort_run(db, business_user, cfg, n=20, held=10, hours_ago=1)
    before = _yield(run)["run"]
    await db.execute(text("UPDATE results SET party_name = 'NAMED LATER' WHERE job_id = :j"), {"j": run})
    await db.commit()
    assert _yield(run)["run"] == before == {SEATTLE: {"n": 20, "held": 10}}


@pytest.mark.asyncio
async def test_over_quota_rows_passed_the_gate(db, business_user):
    cfg = await _config(db, business_user)
    await _cohort_run(db, business_user, cfg, n=60, held=6)
    run = await _cohort_run(db, business_user, cfg, n=20, held=2, over_quota=15, hours_ago=1)
    warnings, _ = qq.evaluate_yield(_yield(run))
    assert warnings == []
    assert _yield(run)["run"][SEATTLE] == {"n": 20, "held": 2}


@pytest.mark.asyncio
async def test_each_jurisdiction_is_judged_against_its_own_baseline(db, business_user):
    """A healthy Bellevue history neither alarms nor vouches for a Seattle run."""
    cfg = await _config(db, business_user)
    await _cohort_run(db, business_user, cfg, n=60, held=0, spec=bellevue)
    run = await _cohort_run(db, business_user, cfg, n=20, held=20, hours_ago=1)
    warnings, unjudged = qq.evaluate_yield(_yield(run))
    assert warnings == [] and unjudged == [SEATTLE]


@pytest.mark.asyncio
async def test_pre_gate_runs_are_not_a_baseline(db, business_user):
    cfg = await _config(db, business_user)
    old = await _job(db, business_user, cfg, started=qq.GATE_DEPLOYED_AT - timedelta(hours=2))
    for _ in range(60):
        await _row(db, old, business_user.id, seattle())
    run = await _cohort_run(db, business_user, cfg, n=20, held=20, hours_ago=1)
    assert _yield(run)["baseline"] == {}


@pytest.mark.asyncio
async def test_check_job_pages_once_per_collapsed_jurisdiction(db, business_user, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    cfg = await _config(db, business_user)
    await _cohort_run(db, business_user, cfg, n=60, held=6)
    run = await _cohort_run(db, business_user, cfg, n=20, held=19, hours_ago=1)
    report = _measure(lambda s: dq.check_job(s, run))
    assert [w["kind"] for w in report["warnings"]].count("qualification_collapse") == 1
    assert report["qualification"]["run"] == {SEATTLE: {"n": 20, "held": 19}}
    details = await _audit(f"qualification_yield:king/WA/code_violation/{SEATTLE}")
    assert details and "passed 5.0% vs 90.0% baseline" in details[0]


@pytest.mark.asyncio
async def test_other_record_types_carry_no_qualification_measure(db, business_user):
    cfg = await _config(db, business_user, record_type="probate")
    job = await _job(db, business_user, cfg)
    report = _measure(lambda s: dq.check_job(s, job, alert=False))
    assert report["qualification"] is None


@pytest.mark.asyncio
async def test_the_sweep_runs_the_gate_check(db, business_user, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _row(db, job, business_user.id, _held())
    stats = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
    assert stats["gate_breaches"] >= 1


# ── Codex review r1 (P2 threshold, P3 coverage) ──────────────────────────────

def _measured(run_n, run_held, base_n=60, base_held=6) -> dict:
    return {"run": {SEATTLE: {"n": run_n, "held": run_held}},
            "baseline": {SEATTLE: {"n": base_n, "held": base_held}}, "reasons": {}}


def test_a_drop_of_exactly_the_threshold_warns_and_just_under_does_not():
    assert qq.DROP_POINTS == 30.0
    [w] = qq.evaluate_yield(_measured(10, 4))[0]                 # 90% -> 60%: exactly 30
    assert (w["run_pct"], w["baseline_pct"]) == (60.0, 90.0)
    assert qq.evaluate_yield(_measured(10, 3))[0] == []          # 90% -> 70%
    # Unrounded: 66.67% (40/60) vs 36.7% (367/1000) is a 29.97-point drop; the rounded
    # figures (66.7 - 36.7) would read exactly 30 and warn.
    assert qq.evaluate_yield(_measured(1000, 633, base_n=60, base_held=20))[0] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["submitting", "unmatched"])
async def test_every_spent_skip_trace_status_on_a_held_row_is_a_breach(db, business_user, status):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _row(db, job, business_user.id, _held(), mark=NOT_QUALIFIED)
    db.add(PendingSkipTraceRow(id=str(uuid.uuid4()), job_id=job, result_id=rid, user_id=business_user.id,
                               property_address="4512 S HOLLY ST", status=status))
    await db.commit()
    assert _mine(_measure(qq.gate_invariants), job)["outbound_held"] == {job: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["null", '"a string"', "[1, 2]"])
async def test_a_null_or_scalar_enrichment_reads_as_unstamped_never_crashes(db, business_user, value):
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    rid = await _row(db, job, business_user.id, seattle())
    await db.execute(text(f"UPDATE results SET enrichment_data = '{value}'::json WHERE id = :i"), {"i": rid})
    await db.commit()
    found = _mine(_measure(qq.gate_invariants), job)
    assert found["unstamped"] == {job: 1} and found["unheld_unqualified"] == {}


def test_the_judged_marker_is_versioned():
    assert dq._checked_key("abc") == "bl:dq:checked:v2:abc"


@pytest.mark.asyncio
async def test_a_failing_gate_check_never_breaks_the_sweep(db, business_user, monkeypatch):
    cfg = await _config(db, business_user)
    await _job(db, business_user, cfg)

    def boom(*a, **k):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(qq, "gate_invariants", boom)
    stats = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
    assert stats["checked"] >= 1 and "gate_breaches" not in stats


@pytest.mark.asyncio
async def test_the_gate_check_still_runs_after_a_coverage_check_aborts_its_transaction(
        db, business_user, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")
    cfg = await _config(db, business_user)
    job = await _job(db, business_user, cfg)
    await _row(db, job, business_user.id, _held())

    def aborts(db_, job_id, **kw):
        db_.execute(text("SELECT 1/0"))  # leaves the session's transaction aborted

    monkeypatch.setattr(dq, "check_job", aborts)
    stats = dq.run_data_quality_sweep(lookback_hours=2, limit=500)
    assert stats["checked"] == 0 and stats["gate_breaches"] >= 1
