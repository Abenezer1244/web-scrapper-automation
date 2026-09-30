"""The claim with a contact-lookup ACTION (Phase 1b-2, 2a-ii).

`claim_skip_trace_rows(..., action_id=...)` writes the action onto every row it
queues, and claims a lead only when that action belongs to the claim's own tenant AND
job (W6), so one job's spend can never be attributed to another's action. With no
action (the scrape path) the statement is the one it always was. The claim also
reports WHICH leads it held (W5), so the action worker can write a verdict per lead.

Real PG, real claim, real locks; no Tracerfy.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text

from src.config import settings
from src.db import session as db_session
from src.db.session import system_sync_session
from tests.test_audit4_paid_skip_trace_gate import _account, _payloads
from tests.test_skip_trace_claim import _locked_claim

_TRIAL = {"trial_ends_at": datetime.now(UTC) + timedelta(days=5)}


async def _action_for(db, user_id: str, job_id: str) -> str:
    aid = str(uuid.uuid4())
    await db.execute(text(
        "INSERT INTO contact_lookup_actions (id, user_id, job_id, category, quote_id, status, "
        "unit_price_cents, currency, pricing_version) VALUES "
        "(:a, :u, :j, 'new', :q, 'running', 8, 'USD', '2026-06')"
    ), {"a": aid, "u": user_id, "j": job_id, "q": f"q-{aid}"})
    await db.commit()
    return aid


async def _rows(db, result_ids) -> list:
    return (await db.execute(text(
        "SELECT p.result_id::text AS rid, p.action_id::text AS action_id, r.skip_trace_status "
        "FROM results r LEFT JOIN pending_skip_trace_rows p ON p.result_id = r.id "
        "WHERE r.id = ANY(CAST(:ids AS uuid[])) ORDER BY r.id"
    ), {"ids": [str(i) for i in result_ids]})).all()


def _claim(payloads, **kw):
    with system_sync_session() as s:
        won = _locked_claim(s, payloads, **kw)
        s.commit()
    return won


async def test_an_action_claim_writes_the_action_on_every_row(db):
    user = await _account(db)
    payloads = await _payloads(db, user, 3)
    action = await _action_for(db, user.id, str(payloads[0]["job_id"]))
    won = _claim(payloads, action_id=action)
    assert len(won) == 3
    rows = await _rows(db, [p["result_id"] for p in payloads])
    assert {(r.action_id, r.skip_trace_status) for r in rows} == {(action, "queued")}


async def test_the_scrape_path_still_writes_no_action(db):
    user = await _account(db)
    payloads = await _payloads(db, user, 2)
    assert len(_claim(payloads)) == 2
    rows = await _rows(db, [p["result_id"] for p in payloads])
    assert {r.action_id for r in rows} == {None}


async def test_the_scrape_paths_statement_does_not_mention_an_action(db):
    """No action -> the INSERT is the scrape path's own, not a variant of it."""
    user = await _account(db)
    payloads = await _payloads(db, user, 1)
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "INSERT INTO pending_skip_trace_rows" in statement:
            seen.append(statement)

    event.listen(db_session.sync_engine, "before_cursor_execute", _record)
    try:
        _claim(payloads)
    finally:
        event.remove(db_session.sync_engine, "before_cursor_execute", _record)
    assert len(seen) == 1
    assert "action" not in seen[0]


async def test_an_action_of_another_job_claims_nothing(db):
    """Same tenant, other job: the join refuses it, so no spend is misattributed."""
    user = await _account(db)
    payloads = await _payloads(db, user, 2)
    other_job_payloads = await _payloads(db, user, 1)
    action = await _action_for(db, user.id, str(other_job_payloads[0]["job_id"]))
    assert _claim(payloads, action_id=action) == []
    rows = await _rows(db, [p["result_id"] for p in payloads])
    assert {(r.rid, r.skip_trace_status) for r in rows} == {(None, "not_attempted")}


async def test_an_action_of_another_tenant_claims_nothing(db):
    user = await _account(db)
    stranger = await _account(db)
    payloads = await _payloads(db, user, 2)
    theirs = await _payloads(db, stranger, 1)
    action = await _action_for(db, stranger.id, str(theirs[0]["job_id"]))
    assert _claim(payloads, action_id=action) == []
    rows = await _rows(db, [p["result_id"] for p in payloads])
    assert {r.skip_trace_status for r in rows} == {"not_attempted"}


async def test_an_action_that_does_not_exist_claims_nothing(db):
    user = await _account(db)
    payloads = await _payloads(db, user, 1)
    assert _claim(payloads, action_id=str(uuid.uuid4())) == []
    rows = await _rows(db, [p["result_id"] for p in payloads])
    assert rows[0].skip_trace_status == "not_attempted"


async def test_the_trial_room_reports_which_leads_it_held(db, monkeypatch):
    """Advanced costs 2 credits: with room for 3, in the CALLER's order the claim keeps
    the first advanced (2) and the normal (1), and holds the second advanced."""
    monkeypatch.setattr(settings, "SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE", 3)
    trial = await _account(db, **_TRIAL)
    payloads = await _payloads(db, trial, 3, trace_type="advanced")
    payloads[1]["trace_type"] = "normal"
    order = [payloads[2], payloads[0], payloads[1]]  # the caller's order decides
    action = await _action_for(db, trial.id, str(payloads[0]["job_id"]))
    report: dict = {}
    won = _claim(order, action_id=action, report=report)
    assert sorted(won) == sorted([str(payloads[2]["result_id"]), str(payloads[1]["result_id"])])
    assert report["access"] == "trial"
    assert report["held"] == 1
    assert report["held_ids"] == [str(payloads[0]["result_id"])]


@pytest.mark.parametrize("plan", ["starter"])
async def test_a_blocked_account_reports_every_lead_as_held(db, plan):
    user = await _account(db, plan=plan)
    payloads = await _payloads(db, user, 3)
    report: dict = {}
    assert _claim(payloads, report=report) == []
    assert report["held"] == 3
    assert report["held_ids"] == [str(p["result_id"]) for p in payloads]


async def test_a_full_account_reports_no_held_ids(db):
    user = await _account(db)
    payloads = await _payloads(db, user, 2)
    report: dict = {}
    _claim(payloads, report=report)
    assert report["held"] == 0 and report["held_ids"] == []
