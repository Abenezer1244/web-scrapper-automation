"""Lead qualification at the delivery boundary: real Postgres, real statements.

Run 1ad28ded (King, 2026-10-17) billed 491 code violations whose owner lookup was
blocked, so the delivered file named nobody. These tests pin the server-side gate:
the SQL twin agrees with the Python one on Postgres; the worker's hold step, the plan
cap, the billing partition, claim transfer, the skip-trace gate, the results API and
the CSV download all refuse an unqualified code violation; rows without the policy
stamp (everything before this change, every other record type) are untouched; one
account's rows never move another's.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from src.api.download_tokens import mint_download_token
from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    NOT_QUALIFIED,
    OVER_QUOTA,
    actionable_condition,
    actionable_sql,
    delivery_excluded_condition,
    withheld_condition,
    withheld_sql,
)
from src.api.lead_qualification import (
    POLICY_CODE_VIOLATION,
    POLICY_KEY,
    is_qualified,
    qualified_sql,
)
from src.api.run_breakdown import read_partition
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig, User
from src.db.session import system_sync_session
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import NO_ADDRESS_NOT_BILLED_SINCE, transfer_undelivered_claims
from src.workers.tasks_helpers.plan_cap import mark_not_qualified_rows, mark_over_quota_rows
from tests.test_lead_qualification import bellevue, seattle

AFTER_RULE = NO_ADDRESS_NOT_BILLED_SINCE + timedelta(days=2)


async def _config(db, user: User, record_type="code_violation") -> ScraperConfig:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King code violations", county="king",
        state="WA", record_type=record_type, fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(cfg)
    await db.commit()
    return cfg


async def _job(db, user, cfg, *, status="enriching", billed_at=None, **kw) -> str:
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status=status,
               trigger="manual", billing_applied_at=billed_at, **kw))
    await db.commit()
    return job_id


async def _row(db, job_id, user_id, spec: dict, **kw) -> str:
    rid = str(uuid.uuid4())
    fields = {k: v for k, v in spec.items() if k not in ("phone", "email")}
    fields.update(kw)
    fields.setdefault("is_duplicate", False)
    fields.setdefault("dedup_hash", uuid.uuid4().hex)
    db.add(Result(id=rid, job_id=job_id, user_id=user_id, **fields))
    await db.commit()
    return rid


async def _reason(db, rid) -> str | None:
    return (await db.execute(text(
        "SELECT enrichment_data->>:k FROM results WHERE id = :i"),
        {"k": DELIVERY_EXCLUDED_KEY, "i": rid})).scalar()


def _worker(fn):
    """Run a worker statement the way the worker does: the sync (psycopg2) system session."""
    with system_sync_session() as sdb:
        out = fn(sdb)
        sdb.commit()
    return out


async def _hold(db, job_id, user_id) -> int:
    await db.commit()
    return _worker(lambda s: mark_not_qualified_rows(s, job_id=job_id, user_id=str(user_id)))


def _unqualified_cases():
    """(label, row) pairs the Python twin rejects, one per reason."""
    return [
        ("no_owner", seattle(party_name=None, ed={"owner_source": None, "owner_pin": None})),
        ("no_mailing", seattle(mailing_address=None)),
        ("ambiguous", seattle(ed={"kc_pin_status": "multiple", "kc_pin": None, "kc_pin_match": None})),
        ("condo", seattle(ed={"kc_pin_match": "condo_complex"})),
        ("label_as_owner", seattle(party_name="Weeds — 4512 S HOLLY ST", ed={"owner_source": None})),
        ("other_parcel_owner", seattle(ed={"owner_pin": "9999999999"})),
        ("wrong_county", bellevue(ed={"owner_source": "pierce_atip"})),
        ("zero_stripped", bellevue(ed={"owner_pin": "123456789"})),
        ("no_case_number", bellevue(ed={"case_number": None})),
        ("no_date", seattle(date_recorded=None)),
        ("placeholder_parcel", bellevue(parcel="(enrichment unavailable)")),
        ("placeholder_case", bellevue(ed={"case_number": "(enrichment unavailable)"})),
        ("dashed_parcel", bellevue(parcel="0123-456789")),
    ]


# ── the SQL twin agrees with the Python twin, on Postgres ────────────────────

@pytest.mark.asyncio
async def test_sql_and_python_twins_agree_on_postgres(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    cases = [("seattle_ok", seattle()), ("bellevue_ok", bellevue()),
             ("llc_ok", bellevue(party_name="HARTLEY FAMILY TRUST")),
             # Edges where Python and Postgres string handling differ (Codex r2): both
             # twins must trim the same characters and read JSON numbers as ->> does.
             ("numeric_case_number", bellevue(ed={"case_number": 25112233})),
             ("padded_owner_pin", bellevue(ed={"owner_pin": " 0123456789\t"})),
             ("nbsp_owner", bellevue(party_name=" ")),
             ("tab_only_owner", bellevue(party_name="\t\n")),
             ("numeric_owner_pin", bellevue(parcel="1000000000", ed={"owner_pin": 1e9})),
             ("array_source", bellevue(ed={"source": ["bellevue_code_enforcement"]})),
             ("object_pin_status", seattle(party_name=None, ed={"kc_pin_status": {"x": 1}})),
             ("bool_case_number", bellevue(ed={"case_number": True})),
             ("array_case_number", bellevue(ed={"case_number": ["25-1"]})),
             ("numeric_case_type", bellevue(ed={"case_type": 7})),
             *_unqualified_cases()]
    ids = {label: await _row(db, job_id, business_user.id, row) for label, row in cases}
    got = dict((await db.execute(text(
        f"SELECT r.id::text, {qualified_sql('r')} FROM results r WHERE r.job_id = :j"),  # noqa: S608
        {"j": job_id})).all())
    for label, row in cases:
        assert got[ids[label]] is is_qualified(row), label
    assert sum(got.values()) == 5  # the three ok rows + numeric case number + padded owner_pin


# ── the hold step ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hold_marks_only_stamped_unqualified_rows(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    good = await _row(db, job_id, business_user.id, seattle())
    no_owner = await _row(db, job_id, business_user.id, seattle(party_name=None))
    no_address = await _row(db, job_id, business_user.id,
                            seattle(property_address=None, mailing_address=None))
    legacy_spec = seattle(party_name=None)
    legacy_spec["enrichment_data"] = {k: v for k, v in legacy_spec["enrichment_data"].items()
                                      if k != POLICY_KEY}
    legacy = await _row(db, job_id, business_user.id, legacy_spec)

    # The count is the rows with an address: the run log names no-address rows itself.
    assert await _hold(db, job_id, business_user.id) == 1
    assert await _reason(db, no_owner) == NOT_QUALIFIED
    # Held too, so a sweep that later finds its address and owner cannot put it in this
    # finished run (Codex final pass); the breakdown still files it under no address.
    assert await _reason(db, no_address) == NOT_QUALIFIED
    for rid in (good, legacy):
        assert await _reason(db, rid) is None
    part = await db.run_sync(lambda s: read_partition(s, job_id, business_user.id))
    assert (part.new, part.not_qualified, part.no_address) == (2, 1, 1)
    # The sweep fills the address and owner later: the finished run still lists nothing new.
    row = await db.get(Result, no_address)
    row.property_address, row.mailing_address = "4512 S HOLLY ST", "4512 S HOLLY ST, SEATTLE, WA 98118"
    await db.commit()
    assert (await db.execute(select(func.count()).select_from(Result).where(
        Result.id == no_address, actionable_condition()))).scalar() == 0


@pytest.mark.asyncio
async def test_rerun_redecides_a_row_enrichment_completed(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    rid = await _row(db, job_id, business_user.id,
                     seattle(party_name=None, ed={"owner_source": None, "owner_pin": None}))
    assert await _hold(db, job_id, business_user.id) == 1
    # The watchdog re-run's enrichment names the owner before billing.
    row = await db.get(Result, rid)
    row.party_name = "HARTLEY MARGARET A"
    row.enrichment_data = {**row.enrichment_data, "owner_source": "king_erealproperty",
                           "owner_pin": "1234567890"}
    await db.commit()
    assert await _hold(db, job_id, business_user.id) == 0
    assert await _reason(db, rid) is None
    # And holding again is a no-op for an unchanged job.
    assert await _hold(db, job_id, business_user.id) == 0


@pytest.mark.asyncio
async def test_held_survivor_holds_its_same_run_siblings(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    h = uuid.uuid4().hex
    survivor = await _row(db, job_id, business_user.id, seattle(mailing_address=None), dedup_hash=h)
    # A sibling that itself passes would otherwise deliver the property through lists.
    sibling = await _row(db, job_id, business_user.id, seattle(), dedup_hash=h,
                         is_duplicate=True, duplicate_reason="same_run")
    assert await _hold(db, job_id, business_user.id) == 1
    assert await _reason(db, survivor) == NOT_QUALIFIED
    assert await _reason(db, sibling) == NOT_QUALIFIED


@pytest.mark.asyncio
async def test_sibling_hold_skips_an_unstamped_sibling(db, business_user):
    # A watchdog re-run of a job started before the stamp shipped mixes both kinds.
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    h = uuid.uuid4().hex
    await _row(db, job_id, business_user.id, seattle(mailing_address=None), dedup_hash=h)
    legacy = seattle()
    legacy["enrichment_data"] = {k: v for k, v in legacy["enrichment_data"].items() if k != POLICY_KEY}
    sibling = await _row(db, job_id, business_user.id, legacy, dedup_hash=h,
                         is_duplicate=True, duplicate_reason="same_run")
    assert await _hold(db, job_id, business_user.id) == 1
    assert await _reason(db, sibling) is None


@pytest.mark.asyncio
async def test_hold_never_touches_another_account(db, business_user, starter_user):
    cfg_a = await _config(db, business_user)
    cfg_b = await _config(db, starter_user)
    job_a = await _job(db, business_user, cfg_a)
    job_b = await _job(db, starter_user, cfg_b)
    theirs = await _row(db, job_b, starter_user.id, seattle(party_name=None))
    # Even asked with the other account's job id, the user filter refuses it.
    assert _worker(lambda s: mark_not_qualified_rows(
        s, job_id=job_b, user_id=str(business_user.id))) == 0
    assert await _hold(db, job_a, business_user.id) == 0
    assert await _reason(db, theirs) is None


# ── quota: never spent on a held row, never charged for one ──────────────────

@pytest.mark.asyncio
async def test_cap_spends_quota_only_on_qualified_rows(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    # The held row ranks first by date (open, newest) and would take the only slot.
    held = await _row(db, job_id, business_user.id,
                      seattle(party_name=None, date_recorded="10/20/2026"))
    good = await _row(db, job_id, business_user.id, seattle(date_recorded="10/01/2026"))
    good2 = await _row(db, job_id, business_user.id, seattle(date_recorded="09/01/2026"))
    await _hold(db, job_id, business_user.id)
    capped = _worker(lambda s: mark_over_quota_rows(
        s, job_id=job_id, user_id=str(business_user.id), remaining=1,
        record_type="code_violation"))
    assert capped == [good2]
    assert await _reason(db, held) == NOT_QUALIFIED and await _reason(db, good) is None
    part = await db.run_sync(lambda s: read_partition(s, job_id, business_user.id))
    # Billing = partition.new: one qualified lead, the held row never charged.
    assert (part.new, part.not_qualified, part.over_quota, part.unclassified) == (1, 1, 1, 0)


@pytest.mark.asyncio
async def test_belt_refuses_an_unqualified_row_even_without_the_mark(db, business_user):
    # If the hold step were skipped, billing, the list and the export still refuse it.
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    await _row(db, job_id, business_user.id, seattle(party_name=None))
    part = await db.run_sync(lambda s: read_partition(s, job_id, business_user.id))
    assert (part.new, part.not_qualified) == (0, 1)
    assert (await db.execute(select(func.count()).select_from(Result).where(
        Result.job_id == job_id, actionable_condition()))).scalar() == 0
    assert (await db.execute(text(
        f"SELECT count(*) FROM results r WHERE r.job_id = :j AND {actionable_sql('r')}"),  # noqa: S608
        {"j": job_id})).scalar() == 0


@pytest.mark.asyncio
async def test_other_record_types_are_unaffected(db, business_user):
    cfg = await _config(db, business_user, record_type="probate")
    job_id = await _job(db, business_user, cfg)
    await _row(db, job_id, business_user.id, {
        "party_name": None, "property_address": "1 A ST", "mailing_address": None,
        "enrichment_data": {"source": "king_probate"}})
    assert await _hold(db, job_id, business_user.id) == 0
    part = await db.run_sync(lambda s: read_partition(s, job_id, business_user.id))
    assert (part.new, part.not_qualified) == (1, 0)


# ── skip trace never buys a held lead ────────────────────────────────────────

@pytest.mark.asyncio
async def test_skip_trace_exclusion_covers_held_rows(db, business_user):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg)
    held = await _row(db, job_id, business_user.id, seattle(mailing_address=None))
    capped = await _row(db, job_id, business_user.id, seattle(),
                        enrichment_data={**seattle()["enrichment_data"], DELIVERY_EXCLUDED_KEY: OVER_QUOTA})
    clean = await _row(db, job_id, business_user.id, seattle())
    await _hold(db, job_id, business_user.id)
    # Added after the hold ran: no mark yet, still never bought.
    unmarked = await _row(db, job_id, business_user.id, seattle(party_name=None))
    excluded = set((await db.execute(select(Result.id).where(
        Result.job_id == job_id, delivery_excluded_condition()))).scalars())
    assert excluded == {held, capped}
    withheld = set((await db.execute(select(Result.id).where(
        Result.job_id == job_id, withheld_condition()))).scalars())
    assert withheld == {held, capped, unmarked} and clean not in withheld
    raw = set((await db.execute(text(
        f"SELECT r.id::text FROM results r WHERE r.job_id = :j AND {withheld_sql('r')}"),  # noqa: S608
        {"j": job_id})).scalars())
    assert raw == withheld


# ── stay held, the next run delivers and bills (owner, 2026-10-25) ───────────

@pytest.mark.asyncio
async def test_next_run_takes_the_claim_of_a_held_lead(db, business_user):
    cfg = await _config(db, business_user)
    spec = bellevue()
    h = legacy_strong_signature(spec["parcel_id"], spec["property_address"])
    assert h is not None
    old_job = await _job(db, business_user, cfg, status="done", billed_at=AFTER_RULE)
    held = await _row(db, old_job, business_user.id, bellevue(mailing_address=None), dedup_hash=h,
                      enrichment_data={**spec["enrichment_data"], DELIVERY_EXCLUDED_KEY: NOT_QUALIFIED})
    db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=business_user.id, dedup_hash=h,
                           first_result_id=held, first_job_id=old_job,
                           parcel_id=spec["parcel_id"], property_address=spec["property_address"]))
    await db.commit()
    # A sweep names the mailing later: the finished run still does not deliver it.
    row = await db.get(Result, held)
    row.mailing_address = "PO BOX 4401, BELLEVUE, WA 98009"
    await db.commit()
    assert (await db.execute(select(func.count()).select_from(Result).where(
        Result.job_id == old_job, actionable_condition()))).scalar() == 0

    new_job = await _job(db, business_user, cfg)
    new_row = await _row(db, new_job, business_user.id, bellevue(), dedup_hash=h,
                         is_duplicate=True, duplicate_reason="prior_run",
                         duplicate_source_job_id=old_job)
    moved = _worker(lambda s: transfer_undelivered_claims(
        s, new_job, business_user.id, "code_violation"))
    assert moved == 1
    fresh = await db.get(Result, new_row)
    await db.refresh(fresh)
    assert fresh.is_duplicate is False
    part = await db.run_sync(lambda s: read_partition(s, new_job, business_user.id))
    assert part.new == 1


@pytest.mark.asyncio
async def test_an_unqualified_copy_does_not_take_the_claim(db, business_user):
    cfg = await _config(db, business_user)
    spec = bellevue()
    h = legacy_strong_signature(spec["parcel_id"], spec["property_address"])
    old_job = await _job(db, business_user, cfg, status="done", billed_at=AFTER_RULE)
    held = await _row(db, old_job, business_user.id, spec, dedup_hash=h,
                      enrichment_data={**spec["enrichment_data"], DELIVERY_EXCLUDED_KEY: NOT_QUALIFIED})
    db.add(DeliveredRecord(id=str(uuid.uuid4()), user_id=business_user.id, dedup_hash=h,
                           first_result_id=held, first_job_id=old_job,
                           parcel_id=spec["parcel_id"], property_address=spec["property_address"]))
    await db.commit()
    new_job = await _job(db, business_user, cfg)
    await _row(db, new_job, business_user.id, bellevue(mailing_address=None), dedup_hash=h,
               is_duplicate=True, duplicate_reason="prior_run", duplicate_source_job_id=old_job)
    moved = _worker(lambda s: transfer_undelivered_claims(
        s, new_job, business_user.id, "code_violation"))
    assert moved == 0


# ── the customer surfaces: results list and CSV ──────────────────────────────

@pytest.mark.asyncio
async def test_results_and_download_exclude_held_rows(client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg, status="done", billed_at=AFTER_RULE,
                        export_key="exports/x.csv", record_count=1, billed_count=1)
    await _row(db, job_id, business_user.id, seattle(party_name="QUALIFIED OWNER ONE"))
    held = await _row(db, job_id, business_user.id,
                      seattle(party_name=None, property_address="77 HELD ST"))
    await _hold(db, job_id, business_user.id)
    assert await _reason(db, held) == NOT_QUALIFIED

    page = await client.get(f"/jobs/{job_id}/results",
                            headers={"Authorization": f"Bearer {business_token}"})
    assert page.status_code == 200, page.text
    body = page.json()
    assert [i["party_name"] for i in body["items"]] == ["QUALIFIED OWNER ONE"]
    assert body["new_count"] == 1

    token = mint_download_token(str(business_user.id), job_id, ttl_seconds=60)
    dl = await client.get(f"/jobs/{job_id}/download", params={"token": token})
    assert dl.status_code == 200, dl.text
    assert "QUALIFIED OWNER ONE" in dl.text and "77 HELD ST" not in dl.text


@pytest.mark.asyncio
async def test_another_account_cannot_read_a_held_row(client, db, business_user, starter_token):
    cfg = await _config(db, business_user)
    job_id = await _job(db, business_user, cfg, status="done", billed_at=AFTER_RULE)
    await _row(db, job_id, business_user.id, seattle())
    page = await client.get(f"/jobs/{job_id}/results",
                            headers={"Authorization": f"Bearer {starter_token}"})
    assert page.status_code == 404


# ── migration 121 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migration_121_adds_a_nullable_integer_column(db):
    row = (await db.execute(text(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'jobs' "
        "AND column_name = 'breakdown_not_qualified'"))).one()
    assert tuple(row) == ("integer", "YES")


def test_policy_constant_is_what_the_worker_stamps():
    from src.workers.tasks import stamp_qualification_policy
    assert stamp_qualification_policy({})[POLICY_KEY] == POLICY_CODE_VIOLATION
