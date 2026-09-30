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


# The scrape path's INSERT for one lead, whitespace-normalised, as the driver sees it.
# Captured from origin/main's claim BEFORE 2a-ii and proven byte-identical to this
# branch's (with action_id=None), so the paid path every scrape takes cannot drift.
_SCRAPE_INSERT_ONE_ROW = (
    "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, property_addres"
    "s, city, state, zip, first_name, last_name, mail_address, mail_city, mail_state, mai"
    "l_zip, trace_type, status) SELECT v.id, v.job_id, v.result_id, v.user_id, v.property"
    "_address, v.city, v.state, v.zip, v.first_name, v.last_name, v.mail_address, v.mail_"
    "city, v.mail_state, v.mail_zip, v.trace_type, 'queued' FROM (VALUES (CAST(%(id_0)s A"
    "S uuid), CAST(%(job_id_0)s AS uuid), CAST(%(result_id_0)s AS uuid), CAST(%(user_id_0"
    ")s AS uuid), CAST(%(property_address_0)s AS text), CAST(%(city_0)s AS text), CAST(%("
    "state_0)s AS text), CAST(%(zip_0)s AS text), CAST(%(first_name_0)s AS text), CAST(%("
    "last_name_0)s AS text), CAST(%(mail_address_0)s AS text), CAST(%(mail_city_0)s AS te"
    "xt), CAST(%(mail_state_0)s AS text), CAST(%(mail_zip_0)s AS text), CAST(%(trace_type"
    "_0)s AS text))) AS v(id, job_id, result_id, user_id, property_address, city, state, "
    "zip, first_name, last_name, mail_address, mail_city, mail_state, mail_zip, trace_typ"
    "e) JOIN public.results r ON r.id = v.result_id AND r.user_id = v.user_id AND r.job_i"
    "d = v.job_id JOIN public.jobs j ON j.id = v.job_id AND j.user_id = v.user_id WHERE r"
    ".user_id = CAST(%(uid)s AS uuid) AND j.user_id = CAST(%(uid)s AS uuid) AND r.skip_tr"
    "ace_status = %(claimable)s ON CONFLICT (result_id) WHERE status IN ('queued', 'submi"
    "tting', 'submitted') DO NOTHING RETURNING result_id"
)
_SCRAPE_PARAM_KEYS = {
    "uid", "claimable", "id_0", "job_id_0", "result_id_0", "user_id_0",
    "property_address_0", "city_0", "state_0", "zip_0", "first_name_0", "last_name_0",
    "mail_address_0", "mail_city_0", "mail_state_0", "mail_zip_0", "trace_type_0",
}


async def test_the_scrape_paths_statement_and_params_are_unchanged(db):
    """No action -> EXACTLY the statement and bind parameters the scrape path always ran."""
    user = await _account(db)
    payloads = await _payloads(db, user, 1)
    seen: list = []

    def _record(conn, cursor, statement, params, context, executemany):
        if "INSERT INTO pending_skip_trace_rows" in statement:
            seen.append((" ".join(statement.split()), set(params)))

    event.listen(db_session.sync_engine, "before_cursor_execute", _record)
    try:
        _claim(payloads)
    finally:
        event.remove(db_session.sync_engine, "before_cursor_execute", _record)
    assert len(seen) == 1
    assert seen[0][0] == _SCRAPE_INSERT_ONE_ROW
    assert seen[0][1] == _SCRAPE_PARAM_KEYS


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


async def test_an_action_claim_across_insert_chunks_marks_every_row(db, monkeypatch):
    """Chunking rebuilds the parameters per statement; the action must ride every chunk."""
    import src.workers.skip_trace_claim as claim_mod

    monkeypatch.setattr(claim_mod, "_INSERT_CHUNK_ROWS", 2)
    user = await _account(db)
    payloads = await _payloads(db, user, 5)
    action = await _action_for(db, user.id, str(payloads[0]["job_id"]))
    assert len(_claim(payloads, action_id=action)) == 5
    rows = await _rows(db, [p["result_id"] for p in payloads])
    assert {(r.action_id, r.skip_trace_status) for r in rows} == {(action, "queued")}


async def test_an_action_claims_withdrawn_row_leaves_no_trace_of_the_action(db, monkeypatch):
    """A lead settled by another writer between the INSERT and the results UPDATE is
    withdrawn by OUR pending id, in the same transaction, across chunks, with an action."""
    import psycopg2

    import src.workers.skip_trace_claim as claim_mod
    from src.workers.skip_trace_claim import claim_skip_trace_rows, lock_job_for_claim

    monkeypatch.setattr(claim_mod, "_INSERT_CHUNK_ROWS", 2)
    user = await _account(db)
    payloads = await _payloads(db, user, 5)
    job_id = str(payloads[0]["job_id"])
    action = await _action_for(db, user.id, job_id)
    victim = sorted(str(p["result_id"]) for p in payloads)[0]  # first chunk (sorted by id)

    def _settle_victim() -> None:
        other = psycopg2.connect(
            settings.DATABASE_URL_SYNC.replace("postgresql+psycopg2://", "postgresql://"))
        try:
            other.cursor().execute(
                "UPDATE results SET skip_trace_status = 'hit' WHERE id = %s", (victim,))
            other.commit()
        finally:
            other.close()

    with system_sync_session() as s:
        lock_job_for_claim(s, job_id)
        original, state = s.execute, {"inserts": 0, "fired": False}

        def _execute(statement, *args, **kwargs):
            if (state["inserts"] and not state["fired"]
                    and "UPDATE results SET skip_trace_status" in str(statement)):
                state["fired"] = True
                _settle_victim()
            result = original(statement, *args, **kwargs)
            if "INSERT INTO pending_skip_trace_rows" in str(statement):
                state["inserts"] += 1
            return result

        s.execute = _execute
        try:
            won = claim_skip_trace_rows(s, payloads, action_id=action)
        finally:
            s.execute = original
        s.commit()
    assert state["inserts"] > 1 and state["fired"]
    assert victim not in won and len(won) == 4
    rows = {r.rid: r for r in await _rows(db, [p["result_id"] for p in payloads]) if r.rid}
    assert victim not in rows, "the withdrawn lead kept a pending row"
    assert {(r.action_id, r.skip_trace_status) for r in rows.values()} == {(action, "queued")}
