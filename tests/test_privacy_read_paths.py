"""A row withheld by a privacy rule (migration 126) is in no list, count, file or rule.

Phase 6 P3a. The flag is set the way the enforcer will set it (P5): both columns,
pointing at a real rule row. The rule row is 'pending', the only state the database
allows until P7; the READ rules key on the result flag alone, so that is enough here.

Real database and real endpoints (conftest fixtures). Synthetic rows only.
"""
import csv
import io
import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import text

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    NOT_QUALIFIED,
    actionable_sql,
    deliverable_before_cap_sql,
    is_actionable,
    withheld_sql,
)
from src.db.models import Job, Result, ScraperConfig, User

NOW = datetime.now(UTC)


async def _job(db, user: User, config: ScraperConfig) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(
        id=job_id, user_id=user.id, scraper_config_id=config.id, status="done",
        trigger="manual", record_count=0, billed_count=0, records_found=None,
        retry_count=0, created_at=NOW, export_key=f"exports/{job_id}.csv",
    ))
    await db.commit()
    return job_id


async def _row(db, job_id: str, user_id: str, *, tag: str, prop: str | None = None,
               duplicate: bool = False, reason: str | None = None,
               held: bool = False) -> str:
    row_id = str(uuid.uuid4())
    enrichment = {"instrument_number": tag}
    if held:
        enrichment[DELIVERY_EXCLUDED_KEY] = NOT_QUALIFIED
    db.add(Result(
        id=row_id, job_id=job_id, user_id=user_id, party_name=f"OWNER {tag}",
        property_address=prop, dedup_hash=uuid.uuid4().hex, is_duplicate=duplicate,
        duplicate_reason=reason, doc_type="Transfer on Death Deed", enrichment_data=enrichment,
    ))
    await db.commit()
    return row_id


async def _withhold(db, *result_ids: str) -> None:
    """Flag rows the way the enforcer will: both columns, a real (pending) rule row."""
    request_id = (await db.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id"))).scalar()
    rule_row = (await db.execute(text(
        "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
        "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
        "VALUES (gen_random_uuid(), :r, 'property', :h, 'person', :h, 'subject', 'v1', "
        "'n3', 'pending') RETURNING id"), {"r": request_id, "h": "c" * 64})).scalar()
    await db.execute(text(
        "UPDATE results SET privacy_suppressed_at = now(), privacy_suppression_id = :s "
        "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"s": rule_row, "ids": list(result_ids)})
    await db.commit()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _results(client: AsyncClient, job_id: str, token: str, **params) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", params=params, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _fixture(db, user: User, config: ScraperConfig) -> tuple[str, dict, dict]:
    """One row of each list category, shown and withheld. Returns (job, shown, withheld)."""
    job = await _job(db, user, config)
    uid = user.id
    shown = {
        "new": await _row(db, job, uid, tag="NEW", prop="1 FIR LN"),
        "already_delivered": await _row(db, job, uid, tag="DLV", prop="2 OAK AVE",
                                        duplicate=True, reason="prior_run"),
        "no_address": await _row(db, job, uid, tag="BARE"),
        "not_qualified": await _row(db, job, uid, tag="HELD", prop="3 ELM ST", held=True),
    }
    withheld = {
        "new": await _row(db, job, uid, tag="XNEW", prop="11 FIR LN"),
        "already_delivered": await _row(db, job, uid, tag="XDLV", prop="12 OAK AVE",
                                        duplicate=True, reason="prior_run"),
        "no_address": await _row(db, job, uid, tag="XBARE"),
        "not_qualified": await _row(db, job, uid, tag="XHELD", prop="13 ELM ST", held=True),
    }
    await _withhold(db, *withheld.values())
    return job, shown, withheld


async def test_a_withheld_row_is_in_no_list_and_no_tab_count(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    job, shown, _ = await _fixture(db, starter_user, scraper_config)
    for category, row_id in shown.items():
        body = await _results(client, job, starter_token, category=category)
        assert [r["id"] for r in body["items"]] == [row_id], category
        assert body["total"] == 1, category
    head = await _results(client, job, starter_token)
    assert head["new_count"] == 1
    assert head["already_delivered_count"] == 1
    assert head["no_address_count"] == 1
    assert head["not_qualified_count"] == 1


async def test_a_withheld_row_is_not_in_the_download(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    job, shown, _ = await _fixture(db, starter_user, scraper_config)
    for category, tag in (("new", "OWNER NEW"), ("already_delivered", "OWNER DLV")):
        resp = await client.get(f"/jobs/{job}/download", params={"category": category},
                                headers=_auth(starter_token))
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert tag in body, category
        assert "OWNER X" not in body, category  # no withheld row's name, in any column
        rows = list(csv.reader(io.StringIO(body)))
        assert len(rows) == 2, category  # header + the one shown lead


async def test_the_sql_rules_refuse_a_withheld_row_and_keep_the_rest(
    starter_user: User, scraper_config: ScraperConfig, db,
):
    """actionable_sql (billing, segments, batch export, dialer), the plan cap's ranking
    and withheld_sql (paid lookups) over the same rows."""
    job, shown, withheld = await _fixture(db, starter_user, scraper_config)

    async def ids(predicate: str) -> set[str]:
        rows = await db.execute(text(
            f"SELECT r.id::text FROM results r WHERE r.job_id = CAST(:j AS uuid) "  # noqa: S608 -- code constants only
            f"AND {predicate}"), {"j": job})
        return {r[0] for r in rows}

    assert await ids(actionable_sql("r")) == {shown["new"], shown["already_delivered"]}
    assert await ids(deliverable_before_cap_sql("r")) == {
        shown["new"], shown["already_delivered"]}
    assert set(withheld.values()) <= await ids(withheld_sql("r"))
    assert shown["new"] not in await ids(withheld_sql("r"))


def test_the_python_twin_refuses_a_flagged_row_and_ignores_unsaved_records():
    flagged = Result(property_address="1 FIR LN", enrichment_data={},
                     privacy_suppressed_at=NOW)
    plain = Result(property_address="1 FIR LN", enrichment_data={})
    assert is_actionable(flagged) is False
    assert is_actionable(plain) is True
    # A scraped record not yet saved carries no flag and is judged as before.
    assert is_actionable({"property_address": "1 FIR LN", "enrichment_data": {}}) is True
