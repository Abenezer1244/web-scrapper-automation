"""The code violations a run held back must be a set the user can open, with reasons.

Phase 1 (lead_qualification.py) holds a code violation back when its owner, parcel or
mailing address is not confirmed: never listed, exported or billed. The run breakdown
says "N held back", so ``GET /jobs/{id}/results?category=not_qualified`` lists them,
``not_qualified_count`` counts them and each row says why (qualification_reasons).

Run 1ad28ded's "already delivered" tab listed 4,915 duplicate copies that were never
enriched, so every owner read N/A although the earlier run had delivered it named.
The already_delivered list now shows a copy's blank fields from that original.

Real DB, real endpoints (conftest fixtures, no mocks):
  - the list is exactly the held rows: marked, with an address, not a same-run sibling,
    including a held row a later run took over and delivered ('superseded');
  - reasons and state come from the row as it is now;
  - the list and its count refuse to be a CSV, export or contact lookup;
  - an already-delivered copy shows the original's fields only where it is blank, from
    one original, on the response only (the stored row and the CSV are unchanged);
  - another account sees none of it.
"""
import uuid

import pytest
from sqlalchemy import text

from src.api import lead_qualification as lq
from src.api.download_tokens import mint_download_token
from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY
from src.api.schemas import QualificationReason, QualificationState
from tests.test_lead_qualification import PIN, seattle
from tests.test_lead_qualification_worker import AFTER_RULE, _config, _hold, _job, _row


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _results(client, job_id: str, token: str, **params) -> dict:
    resp = await client.get(f"/jobs/{job_id}/results", params=params, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _done_job(db, user, cfg) -> str:
    return await _job(db, user, cfg, status="done", billed_at=AFTER_RULE,
                      export_key=f"exports/{uuid.uuid4()}.csv", record_count=0, billed_count=0)


def test_the_api_vocabulary_is_the_policy_vocabulary():
    """The OpenAPI enums the FE is typed from cannot drift from the policy's codes."""
    assert set(QualificationState.__args__) == {
        lq.QUALIFIED, lq.ENRICHMENT_PENDING, lq.NEEDS_REVIEW, lq.NOT_QUALIFIED_STATE}
    assert set(QualificationReason.__args__) == {
        lq.MISSING_OWNER, lq.OWNER_CONFLICT, lq.PARCEL_UNRESOLVED, lq.ADDRESS_AMBIGUOUS,
        lq.MISSING_PROPERTY_ADDRESS, lq.MAILING_UNRESOLVED, lq.INVALID_CASE_DATA,
        lq.UNSUPPORTED_SOURCE}


@pytest.mark.asyncio
async def test_the_held_list_is_the_held_rows_with_their_reasons(
        client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    uid = business_user.id
    good = await _row(db, job, uid, seattle(party_name="QUALIFIED OWNER"))
    no_owner = await _row(db, job, uid, seattle(
        party_name=None, property_address="1 NO OWNER ST",
        ed={"owner_source": None, "owner_pin": None}))
    no_mail = await _row(db, job, uid, seattle(mailing_address=None,
                                               property_address="2 NO MAIL ST"))
    ambiguous = await _row(db, job, uid, seattle(
        party_name=None, property_address="3 CONDO WAY",
        ed={"kc_pin_status": "address_mismatch", "kc_pin": None, "owner_pin": None}))
    # Held but no address at all: the no_address list's, as in the breakdown.
    no_address = await _row(db, job, uid, seattle(
        party_name=None, property_address=None, mailing_address=None))
    # A same-run sibling of a held row: another filing of it, never listed here.
    sibling_hash = uuid.uuid4().hex
    await _row(db, job, uid, seattle(party_name=None, property_address="1 NO OWNER ST"),
               is_duplicate=True, duplicate_reason="same_run", dedup_hash=sibling_hash)
    await db.execute(text("UPDATE results SET dedup_hash = :h WHERE id = :i"),
                     {"h": sibling_hash, "i": no_owner})
    await db.commit()
    await _hold(db, job, uid)

    body = await _results(client, job, business_token, category="not_qualified")
    by_id = {r["id"]: r for r in body["items"]}
    assert set(by_id) == {no_owner, no_mail, ambiguous}
    assert body["total"] == body["not_qualified_count"] == 3
    assert by_id[no_owner]["qualification_reasons"] == ["missing_owner"]
    assert by_id[no_owner]["qualification_state"] == "enrichment_pending"
    assert by_id[no_mail]["qualification_reasons"] == ["mailing_unresolved"]
    assert by_id[ambiguous]["qualification_state"] == "needs_review"
    assert "address_ambiguous" in by_id[ambiguous]["qualification_reasons"]

    # The other lists keep their rows, and the new list's row says it qualifies.
    new = await _results(client, job, business_token)
    assert [(r["id"], r["qualification_state"], r["qualification_reasons"])
            for r in new["items"]] == [(good, "qualified", [])]
    assert {r["id"] for r in (await _results(
        client, job, business_token, category="no_address"))["items"]} >= {no_address}


@pytest.mark.asyncio
async def test_a_held_row_a_later_run_delivered_stays_listed_and_links_to_it(
        client, db, business_user, business_token):
    """The hold is frozen: claim transfer turns the held row into a superseded copy and
    keeps its mark, so this run still names what it held back, and where it went."""
    cfg = await _config(db, business_user)
    held_run = await _done_job(db, business_user, cfg)
    later_run = await _done_job(db, business_user, cfg)
    key = uuid.uuid4().hex
    held = await _row(db, held_run, business_user.id, seattle(party_name=None),
                      dedup_hash=key)
    await _hold(db, held_run, business_user.id)
    await _row(db, later_run, business_user.id, seattle(party_name="NAMED LATER"),
               dedup_hash=key)
    await db.execute(text(
        "UPDATE results SET is_duplicate = true, duplicate_reason = 'superseded', "
        "duplicate_source_job_id = :j WHERE id = :i"), {"j": later_run, "i": held})
    await db.commit()

    body = await _results(client, held_run, business_token, category="not_qualified")
    assert body["not_qualified_count"] == 1
    [row] = body["items"]
    assert row["id"] == held and row["duplicate_reason"] == "superseded"
    assert row["duplicate_source_job_id"] == later_run
    assert row["duplicate_source_available"] is True
    assert row["duplicate_original_visible"] is True
    # Held rows are shown as held: never filled in from the run that delivered them.
    assert row["party_name"] is None and row["filled_from_original"] is None


@pytest.mark.asyncio
async def test_a_row_never_marked_is_not_listed_as_held(client, db, business_user, business_token):
    """The list names what the run HELD. A legacy row without the policy stamp, or an
    over-quota row, is not one of them."""
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    legacy = seattle(party_name=None)
    legacy["enrichment_data"] = {k: v for k, v in legacy["enrichment_data"].items()
                                 if k != lq.POLICY_KEY}
    await _row(db, job, business_user.id, legacy)
    await _row(db, job, business_user.id, seattle(
        party_name=None, ed={DELIVERY_EXCLUDED_KEY: "over_quota"}))
    body = await _results(client, job, business_token, category="not_qualified")
    assert body["total"] == body["not_qualified_count"] == 0


@pytest.mark.asyncio
async def test_held_rows_are_not_a_lead_list(client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    for method, path in (("GET", "export-url"), ("GET", "download"),
                         ("POST", "contact-lookups/quote")):
        if method == "GET":
            resp = await client.get(f"/jobs/{job}/{path}", params={"category": "not_qualified"},
                                    headers=_auth(business_token))
        else:
            resp = await client.post(f"/jobs/{job}/{path}", json={"category": "not_qualified"},
                                     headers=_auth(business_token))
        assert resp.status_code == 422, (path, resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_another_account_cannot_list_my_held_rows(
        client, db, business_user, starter_token):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    await _row(db, job, business_user.id, seattle(party_name=None))
    await _hold(db, job, business_user.id)
    resp = await client.get(f"/jobs/{job}/results", params={"category": "not_qualified"},
                            headers=_auth(starter_token))
    assert resp.status_code == 404


# ── already delivered: the copy shows the lead its earlier run delivered ──────

async def _delivered_pair(db, user, cfg, *, copy_kw: dict, original_kw: dict):
    first = await _done_job(db, user, cfg)
    second = await _done_job(db, user, cfg)
    key = uuid.uuid4().hex
    original = await _row(db, first, user.id, seattle(**original_kw), dedup_hash=key)
    copy = await _row(db, second, user.id, seattle(**copy_kw), dedup_hash=key,
                      is_duplicate=True, duplicate_reason="prior_run",
                      duplicate_source_job_id=first)
    return first, second, original, copy


@pytest.mark.asyncio
async def test_an_unenriched_copy_shows_the_original_owner_and_mailing(
        client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    _first, second, _orig, copy = await _delivered_pair(
        db, business_user, cfg,
        copy_kw={"party_name": None, "mailing_address": "   "},
        original_kw={"party_name": "ORIGINAL OWNER", "parcel_id": PIN,
                     "mailing_address": "PO BOX 9, SEATTLE, WA 98101"})

    body = await _results(client, second, business_token, category="already_delivered")
    [row] = body["items"]
    assert row["id"] == copy
    assert row["party_name"] == "ORIGINAL OWNER"
    assert row["mailing_address"] == "PO BOX 9, SEATTLE, WA 98101"
    assert row["parcel_id"] == PIN
    assert row["filled_from_original"] == ["party_name", "parcel_id", "mailing_address"]
    # Copies describe the earlier delivery, not their own (never enriched) state.
    assert row["qualification_state"] is None and row["qualification_reasons"] is None

    # Response only: the stored copy and the already-delivered CSV are unchanged.
    stored = (await db.execute(text(
        "SELECT party_name, mailing_address, parcel_id FROM results WHERE id = :i"),
        {"i": copy})).one()
    assert tuple(stored) == (None, "   ", None)
    token = mint_download_token(str(business_user.id), second, ttl_seconds=60)
    dl = await client.get(f"/jobs/{second}/download",
                          params={"token": token, "category": "already_delivered"})
    assert dl.status_code == 200, dl.text
    assert "ORIGINAL OWNER" not in dl.text and "PO BOX 9" not in dl.text


@pytest.mark.asyncio
async def test_a_value_on_the_copy_is_never_replaced(client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    _f, second, _o, _c = await _delivered_pair(
        db, business_user, cfg,
        copy_kw={"party_name": "COPY OWNER"},
        original_kw={"party_name": "ORIGINAL OWNER"})
    [row] = (await _results(client, second, business_token, category="already_delivered"))["items"]
    assert row["party_name"] == "COPY OWNER"
    assert row["filled_from_original"] is None


@pytest.mark.asyncio
async def test_a_purged_original_fills_nothing(client, db, business_user, business_token):
    """1,593 of run 1ad28ded's copies point at a purged run: nothing to show them from."""
    cfg = await _config(db, business_user)
    first, second, original, _c = await _delivered_pair(
        db, business_user, cfg, copy_kw={"party_name": None},
        original_kw={"party_name": "GONE OWNER"})
    await db.execute(text("DELETE FROM results WHERE id = :i"), {"i": original})
    await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": first})
    await db.commit()
    [row] = (await _results(client, second, business_token, category="already_delivered"))["items"]
    assert row["party_name"] is None and row["filled_from_original"] is None
    assert row["duplicate_source_job_id"] is None


@pytest.mark.asyncio
async def test_one_original_supplies_every_filled_field(client, db, business_user, business_token):
    """Two originals with one key in one run: the lowest id supplies all the fields,
    never a mix of the two."""
    cfg = await _config(db, business_user)
    first = await _done_job(db, business_user, cfg)
    second = await _done_job(db, business_user, cfg)
    key = uuid.uuid4().hex
    made = [await _row(db, first, business_user.id, seattle(), dedup_hash=key) for _ in range(2)]
    low, high = sorted(made)
    for rid, name, mail in ((low, "FIRST OWNER", "PO BOX 1"), (high, "SECOND OWNER", "2 MAIL ST")):
        await db.execute(text("UPDATE results SET party_name = :n, mailing_address = :m "
                              "WHERE id = :i"), {"n": name, "m": mail, "i": rid})
    await db.commit()
    await _row(db, second, business_user.id, seattle(party_name=None, mailing_address=None),
               dedup_hash=key, is_duplicate=True, duplicate_reason="prior_run",
               duplicate_source_job_id=first)
    [row] = (await _results(client, second, business_token, category="already_delivered"))["items"]
    assert (row["party_name"], row["mailing_address"]) == ("FIRST OWNER", "PO BOX 1")


@pytest.mark.asyncio
async def test_another_accounts_original_is_never_shown(
        client, db, business_user, starter_user, starter_token):
    """A copy whose source id names another account's run fills nothing and links nowhere."""
    theirs = await _config(db, business_user)
    mine = await _config(db, starter_user)
    their_run = await _done_job(db, business_user, theirs)
    my_run = await _done_job(db, starter_user, mine)
    key = uuid.uuid4().hex
    await _row(db, their_run, business_user.id, seattle(party_name="THEIR OWNER"), dedup_hash=key)
    await _row(db, my_run, starter_user.id, seattle(party_name=None), dedup_hash=key,
               is_duplicate=True, duplicate_reason="prior_run", duplicate_source_job_id=their_run)
    [row] = (await _results(client, my_run, starter_token, category="already_delivered"))["items"]
    assert row["party_name"] is None and row["filled_from_original"] is None
    assert row["duplicate_source_job_id"] is None


# ── frozen reasons (#547): why a row WAS held, not what it reads now ─────────

@pytest.mark.asyncio
async def test_a_row_completed_after_the_hold_still_says_why_it_was_held(
        client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    held = await _row(db, job, business_user.id, seattle(mailing_address=None))
    await _hold(db, job, business_user.id)
    # A mailing address found after delivery: the row now passes, the hold stands.
    await db.execute(text("UPDATE results SET mailing_address = 'PO BOX 7, SEATTLE, WA 98101' "
                          "WHERE id = :i"), {"i": held})
    await db.commit()
    [row] = (await _results(client, job, business_token, category="not_qualified"))["items"]
    assert row["qualification_reasons"] == ["mailing_unresolved"]
    assert row["qualification_state"] == "qualified"


@pytest.mark.asyncio
async def test_a_malformed_hold_record_never_fails_the_page(client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    await _row(db, job, business_user.id, seattle(party_name=None, ed={
        DELIVERY_EXCLUDED_KEY: "not_qualified",
        lq.HOLD_KEY: {"reasons": ["missing_owner", "not_a_code", 7], "policy": lq.POLICY_CODE_VIOLATION}}))
    [row] = (await _results(client, job, business_token, category="not_qualified"))["items"]
    assert row["qualification_reasons"] == ["missing_owner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", [
    {"reasons": [], "policy": "code_violation_v1"},
    {"reasons": ["not_a_code"], "policy": "code_violation_v1"},
    {"reasons": ["mailing_unresolved"], "policy": "some_future_policy"},
])
async def test_an_unusable_hold_record_falls_back_to_live_reasons(
        client, db, business_user, business_token, hold):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    await _row(db, job, business_user.id, seattle(party_name=None, ed={
        DELIVERY_EXCLUDED_KEY: "not_qualified", lq.HOLD_KEY: hold}))
    [row] = (await _results(client, job, business_token, category="not_qualified"))["items"]
    assert row["qualification_reasons"] == ["missing_owner"]


@pytest.mark.asyncio
async def test_a_row_held_before_the_record_existed_reads_live_reasons(
        client, db, business_user, business_token):
    cfg = await _config(db, business_user)
    job = await _done_job(db, business_user, cfg)
    await _row(db, job, business_user.id, seattle(party_name=None, ed={DELIVERY_EXCLUDED_KEY: "not_qualified"}))
    [row] = (await _results(client, job, business_token, category="not_qualified"))["items"]
    assert row["qualification_reasons"] == ["missing_owner"]
