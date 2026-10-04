"""UX 3.8s2: the raw-SQL contact paths never leak ciphertext either.

3.8s1 put one decoder (``src/utils/contact_decode.py``) behind every ORM read of a
lead's contact. Three paths read the contact columns with raw ``text()`` SQL, where
the column types never run, and each decrypted on its own with ``decrypt_field``,
which hands an undecryptable value back AS-IS in tolerant mode:

- ``segments._decrypt_pii_rows``: both Segments previews (JSON) and exports (CSV);
- ``batches._leads_page``: the batch leads JSON, batch- and run-scoped;
- ``batch_export._combined_pairs``: the combined batch CSV (download, R2, email).

They now call the decoder with the row's lead id, and pass the raw-SQL scalar
``phone_type`` through ``clean_phone_type`` (no column type cleans it on ``text()``).

Accepted, stated outcome (plan, Codex 3.8 r17 P2): the queries still RANK over
ciphertext before decoding, so a malformed non-NULL contact can win its bucket. The
representative is unchanged, its dropped contact reads null / blank, a WARNING names
the lead and field, and the sibling's valid contact is not substituted. A
decode-aware ranking is backlog.

Every case runs in BOTH ``PII_ENCRYPTION_STRICT`` modes. Real DB (guarded test
database), real requests. Malformed values are seeded with raw SQL bound to the
row's id AND user, with the update count asserted (``_raw_set``).
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

import src.utils.crypto as crypto
from src.config import settings
from src.db.models import (
    BatchRun,
    Job,
    PropertyListMembership,
    Result,
    ScraperBatch,
    ScraperConfig,
    User,
)
from tests.test_contact_decode import (
    CORRUPT_FE1,
    FERNET_SHAPED,
    _enc_json,
    _no_residue,
    _phone,
    _raw_set,
)

pytestmark = pytest.mark.asyncio

NOW = datetime.now(UTC)
PROBATE, PREFC = "probate", "pre_foreclosure"
BOTH = {"record_types": [PREFC, PROBATE]}

VALID = {
    "phone": "2065550100",
    "email": "owner@example.com",
    "phones": [_phone("2065550100"), _phone("2065550101", "Landline"), _phone("2065550102", None)],
    "emails": ["owner@example.com", "second@example.com", "third@example.com"],
    "phone_type": "Mobile",
}
# Every value that must never appear in a body or a log line.
SECRETS = (CORRUPT_FE1, FERNET_SHAPED, "fe1:zz")


@pytest.fixture(params=["tolerant", "strict"])
def mode(request, monkeypatch):
    """Both read modes (the 3.8s1 fixture). The key and the blind index are built
    first, in tolerant mode, because strict refuses the test environment's
    SECRET_KEY-derived key."""
    crypto._instance()
    crypto._blind_index_secret()
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", request.param == "strict")
    return request.param


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ok(resp: Response) -> Response:
    """200, no residue, and the 3.8e no-store still on it."""
    assert resp.status_code == 200, (resp.request.url.path, resp.status_code, resp.text[:300])
    _no_residue(resp.text)
    directives = {d.strip().split("=")[0] for d in resp.headers["cache-control"].split(",")}
    assert "no-store" in directives
    return resp


def _csv_rows(resp: Response) -> list[dict]:
    return list(csv.DictReader(io.StringIO(resp.text)))


# ─── Seeding ─────────────────────────────────────────────────────────────────

async def _job(db: AsyncSession, user: User, record_type: str, *, batch_id: str | None = None,
               age_days: int = 1) -> str:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, batch_id=batch_id, name=f"s2-{record_type}",
        county="pierce", state="WA", record_type=record_type,
        fields=[], enrichment=[], schedule={}, deliver={},
    )
    db.add(cfg)
    await db.flush()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done",
               trigger="batch" if batch_id else "manual",
               created_at=NOW - timedelta(days=age_days)))
    await db.commit()
    return job_id


async def _lead(db: AsyncSession, user: User, job_id: str, party: str, key: str | None,
                *, record_type: str = PROBATE, **contacts) -> str:
    """A lead with NO dates (no filing, auction or tax year), so no derived column in
    the CSV depends on today. Contacts go through the ORM, so they are stored
    encrypted exactly as production stores them. A keyed lead also gets the
    property_list_membership row the worker's dedup step writes, which is what the
    (unwindowed) intersection reads its overlap from."""
    rid = str(uuid.uuid4())
    if key:
        db.add(PropertyListMembership(user_id=user.id, record_type=record_type,
                                      property_key=key, sighting_count=1))
    db.add(Result(
        id=rid, job_id=job_id, user_id=user.id, party_name=party,
        property_address=f"100 {party} ST, TACOMA, WA 98402",
        property_key=key, dedup_hash=uuid.uuid4().hex, is_duplicate=False,
        skip_trace_status="hit" if contacts else None,
        **contacts,
    ))
    await db.commit()
    return rid


async def _batch(db: AsyncSession, user: User, job_ids: list[str], batch_id: str) -> str:
    run_id = str(uuid.uuid4())
    db.add(BatchRun(id=run_id, batch_id=batch_id, user_id=user.id,
                    status="done", child_job_ids=job_ids))
    await db.commit()
    return run_id


async def _new_batch(db: AsyncSession, user: User) -> str:
    batch = ScraperBatch(id=str(uuid.uuid4()), user_id=user.id, name="s2", state="WA",
                         fields=[], enrichment=[], schedule={}, deliver={}, status="active",
                         delivery_mode="everything")
    db.add(batch)
    await db.commit()
    return batch.id


async def _residue_set(db: AsyncSession, user: User, *, batch_id: str | None = None) -> dict:
    """Four properties, each on BOTH lists, each represented by the row named:

    - VALID: every contact valid (scalars, 3-entry arrays, a type);
    - RESIDUE: every contact unreadable: scalars, array entries, phones[].type and
      the scalar phone_type; its partner row has no contacts, so it wins its bucket;
    - BLANK: valid scalars, blank array columns (unreadable: SQL NULL is how "no
      list" is stored);
    - LEGACY: a plaintext phone stored before encryption (tolerant keeps it, strict
      drops it).
    """
    newer = await _job(db, user, PROBATE, batch_id=batch_id, age_days=1)
    older = await _job(db, user, PREFC, batch_id=batch_id, age_days=5)
    ids = {}
    for name, key in (("VALID", "WA|pierce|s2-1"), ("RESIDUE", "WA|pierce|s2-2"),
                      ("BLANK", "WA|pierce|s2-3"), ("LEGACY", "WA|pierce|s2-4")):
        ids[name] = await _lead(db, user, newer, name, key, **VALID)
        await _lead(db, user, older, f"{name} PARTNER", key, record_type=PREFC)
    _raw_set(ids["RESIDUE"], user.id, phone=CORRUPT_FE1, email=FERNET_SHAPED,
             phones=_enc_json([_phone(CORRUPT_FE1), _phone("2065550199", "fe1:zz")]),
             emails=_enc_json([" " + FERNET_SHAPED]), phone_type="fe1:zz")
    _raw_set(ids["BLANK"], user.id, phones="", emails="")
    _raw_set(ids["LEGACY"], user.id, phone="2065550144")
    return {"ids": ids, "jobs": [newer, older]}


def _assert_warned(caplog, lead_id: str, fields: set[str]) -> None:
    lines = [r.getMessage() for r in caplog.records if r.name == "src.utils.contact_decode"]
    for field in fields:
        needle = f"lead={lead_id} field={field}"
        assert any(line.endswith(needle) for line in lines), (field, lines)
    for line in lines:
        for secret in (*SECRETS, *VALID["emails"], "2065550100", "2065550144", "2065550199"):
            assert secret not in line, line


# ─── Segments: both previews and both exports ────────────────────────────────

async def test_segments_previews_and_exports_never_carry_residue(
    mode, client: AsyncClient, db: AsyncSession, business_user: User, business_token: str, caplog,
):
    caplog.set_level(logging.WARNING, logger="src.utils.contact_decode")
    ids = (await _residue_set(db, business_user))["ids"]
    tolerant = mode == "tolerant"
    h = _auth(business_token)

    for path in ("/segments/intersection", "/segments/union"):
        body = _ok(await client.post(path, json=BOTH, headers=h)).json()
        rows = {r["id"]: r for r in body["rows"]}
        assert set(rows) == set(ids.values()), path  # each bucket's representative
        valid = rows[ids["VALID"]]
        assert (valid["phone"], valid["email"], valid["phone_type"]) == (
            VALID["phone"], VALID["email"], VALID["phone_type"])
        residue = rows[ids["RESIDUE"]]
        assert (residue["phone"], residue["email"], residue["phone_type"]) == (None, None, None)
        blank = rows[ids["BLANK"]]
        assert (blank["phone"], blank["email"]) == (VALID["phone"], VALID["email"])
        assert rows[ids["LEGACY"]]["phone"] == ("2065550144" if tolerant else None)

    for path in ("/segments/intersection/export", "/segments/union/export"):
        rows = {r["party_name"]: r for r in _csv_rows(_ok(await client.post(path, json=BOTH, headers=h)))}
        valid = rows["VALID"]
        assert (valid["phone"], valid["phone_2"], valid["phone_3"]) == ("2065550100", "2065550101", "2065550102")
        assert (valid["email"], valid["email_2"], valid["email_3"]) == tuple(VALID["emails"])
        residue = rows["RESIDUE"]
        # Every cell blank: the one readable entry in its phones moves to index 0,
        # which the CSV never prints (phone comes from the scalar, phone_2 from [1]).
        assert (residue["phone"], residue["phone_2"], residue["email"], residue["email_2"]) == ("",) * 4
        blank = rows["BLANK"]
        assert (blank["phone"], blank["phone_2"], blank["email_2"]) == ("2065550100", "", "")
        assert (rows["LEGACY"]["phone"] == "2065550144") is tolerant

    _assert_warned(caplog, ids["RESIDUE"], {"phone", "email", "phones", "phones.type", "emails", "phone_type"})
    _assert_warned(caplog, ids["BLANK"], {"phones", "emails"})


# ─── Batch leads JSON (both scopes) and the combined CSV ─────────────────────

async def test_batch_leads_and_combined_csv_never_carry_residue(
    mode, client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str, caplog,
):
    caplog.set_level(logging.WARNING, logger="src.utils.contact_decode")
    batch_id = await _new_batch(db, starter_user)
    seeded = await _residue_set(db, starter_user, batch_id=batch_id)
    ids = seeded["ids"]
    run_id = await _batch(db, starter_user, seeded["jobs"], batch_id)
    tolerant = mode == "tolerant"
    h = _auth(starter_token)

    for path in (f"/batches/{batch_id}/leads", f"/batches/{batch_id}/runs/{run_id}/leads"):
        leads = {r["id"]: r for r in _ok(await client.get(path, headers=h)).json()["leads"]}
        assert set(leads) == set(ids.values()), path
        valid = leads[ids["VALID"]]
        assert (valid["phone"], valid["email"], valid["phone_type"]) == (
            VALID["phone"], VALID["email"], VALID["phone_type"])
        residue = leads[ids["RESIDUE"]]
        assert (residue["phone"], residue["email"], residue["phone_type"]) == (None, None, None)
        assert leads[ids["LEGACY"]]["phone"] == ("2065550144" if tolerant else None)

    for path in (f"/batches/{batch_id}/download", f"/batches/{batch_id}/runs/{run_id}/download"):
        rows = {r["party_name"]: r for r in _csv_rows(_ok(await client.get(path, headers=h)))}
        valid = rows["VALID"]
        assert (valid["phone"], valid["phone_2"], valid["phone_3"]) == ("2065550100", "2065550101", "2065550102")
        assert (valid["email"], valid["email_2"], valid["email_3"]) == tuple(VALID["emails"])
        residue = rows["RESIDUE"]
        assert residue["phone"] == "" and residue["email"] == "" and residue["email_2"] == ""
        assert (rows["BLANK"]["phone"], rows["BLANK"]["phone_2"]) == ("2065550100", "")
        assert (rows["LEGACY"]["phone"] == "2065550144") is tolerant

    _assert_warned(caplog, ids["RESIDUE"], {"phone", "email", "phones", "phones.type", "emails", "phone_type"})
    _assert_warned(caplog, ids["BLANK"], {"phones", "emails"})


# ─── Tenant isolation ────────────────────────────────────────────────────────

async def test_another_tenants_residue_and_contacts_never_reach_this_tenant(
    mode, client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
    business_user: User, business_token: str,
):
    """Tenant B (starter) holds residue AND valid contacts on the properties; tenant
    A (business, no leads of its own) reads Segments SCOPED to itself (empty, never
    a 404) and gets 404 on B's batch, its runs and its files."""
    batch_id = await _new_batch(db, starter_user)
    seeded = await _residue_set(db, starter_user, batch_id=batch_id)
    run_id = await _batch(db, starter_user, seeded["jobs"], batch_id)
    h = _auth(business_token)
    foreign = (*seeded["ids"].values(), *VALID["emails"], "2065550100", "2065550144")

    for path in ("/segments/intersection", "/segments/union"):
        resp = _ok(await client.post(path, json=BOTH, headers=h))
        assert resp.json()["rows"] == []
        assert not any(v in resp.text for v in foreign)
    for path in ("/segments/intersection/export", "/segments/union/export"):
        resp = _ok(await client.post(path, json=BOTH, headers=h))
        assert _csv_rows(resp) == []  # header only
        assert not any(v in resp.text for v in foreign)
    for path in (f"/batches/{batch_id}/leads", f"/batches/{batch_id}/runs/{run_id}/leads",
                 f"/batches/{batch_id}/download", f"/batches/{batch_id}/runs/{run_id}/download"):
        resp = await client.get(path, headers=h)
        assert resp.status_code == 404, path
        assert not any(v in resp.text for v in foreign)
    # And B still reads its own batch.
    assert (await client.get(f"/batches/{batch_id}/leads", headers=_auth(starter_token))).status_code == 200


# ─── The corrupt representative (accepted, stated outcome) ───────────────────

async def test_a_malformed_contact_can_win_its_bucket_and_reads_absent(
    mode, client: AsyncClient, db: AsyncSession, business_user: User, business_token: str,
):
    """Row A (newer job, malformed scalar phone) and row B (older job, valid phone)
    on one property. Ranking runs over ciphertext, so A is the representative, as it
    was before 3.8s2; its phone is null / blank and B's phone is not substituted."""
    newer = await _job(db, business_user, PROBATE, age_days=1)
    older = await _job(db, business_user, PREFC, age_days=5)
    row_a = await _lead(db, business_user, newer, "ROW A", "WA|pierce|s2-rep", phone="2065550170")
    row_b = await _lead(db, business_user, older, "ROW B", "WA|pierce|s2-rep", record_type=PREFC,
                        phone="2065550171")
    _raw_set(row_a, business_user.id, phone=CORRUPT_FE1)
    h = _auth(business_token)

    for path in ("/segments/intersection", "/segments/union"):
        resp = _ok(await client.post(path, json=BOTH, headers=h))
        [rep] = resp.json()["rows"]
        assert rep["id"] == row_a and rep["phone"] is None, path
        assert row_b not in resp.text and "2065550171" not in resp.text
    for path in ("/segments/intersection/export", "/segments/union/export"):
        resp = _ok(await client.post(path, json=BOTH, headers=h))
        [rep] = _csv_rows(resp)
        assert rep["party_name"] == "ROW A" and rep["phone"] == "", path
        assert "2065550171" not in resp.text


# ─── CSV bytes: canonical values unchanged, padded values trimmed ────────────

# Captured from the code BEFORE 3.8s2 (base c194e1b2), same fixture: one lead with
# valid scalar AND array contacts and no dates, so no column depends on today.
GOLDEN_SEGMENTS_CSV = (
    b'overlap,lists_count,lists,counties,first_name,last_name,phone,phone_type,email,phone_2,phone_3,email_2,email_3,property_street,property_city,property_state,property_zip,filed_date,doc_type,delinquent_amount,delinquent_bill_year,party_name,mailing_address,parcel_id,heirs,legal_description,property_address,lead_subtype,mailing_street,mailing_city,mailing_state,mailing_zip,auction_date,days_to_auction,default_amount\r\n'
    b',1,Probate,pierce,OWNER,GOLDEN,2065550100,Mobile,owner@example.com,2065550101,2065550102,second@example.com,third@example.com,100 GOLDEN OWNER ST,TACOMA,WA,98402,,,,,GOLDEN OWNER,,,,,"100 GOLDEN OWNER ST, TACOMA, WA 98402",,,,,,,,\r\n'
)
GOLDEN_COMBINED_CSV = (
    b'overlap,lists_count,lists,counties,first_name,last_name,phone,phone_type,email,phone_2,phone_3,email_2,email_3,property_street,property_city,property_state,property_zip,filed_date,doc_type,delinquent_amount,delinquent_bill_year,party_name,mailing_address,parcel_id,heirs,legal_description,property_address,lead_subtype,mailing_street,mailing_city,mailing_state,mailing_zip,auction_date,days_to_auction,default_amount\r\n'
    b',1,Probate,pierce,OWNER,GOLDEN,2065550100,Mobile,owner@example.com,2065550101,2065550102,second@example.com,third@example.com,100 GOLDEN OWNER ST,TACOMA,WA,98402,,,,,GOLDEN OWNER,,,,,"100 GOLDEN OWNER ST, TACOMA, WA 98402",,,,,,,,\r\n'
)


async def _canonical_lead(db: AsyncSession, user: User, *, batch_id: str | None = None,
                          contacts: dict | None = None) -> list[str]:
    job_id = await _job(db, user, PROBATE, batch_id=batch_id)
    await _lead(db, user, job_id, "GOLDEN OWNER", "WA|pierce|s2-golden", **(contacts or VALID))
    return [job_id]


async def _segments_csv(client: AsyncClient, token: str) -> Response:
    return _ok(await client.post("/segments/union/export", json={"record_types": [PROBATE]},
                                 headers=_auth(token)))


async def _combined_csv(client: AsyncClient, db: AsyncSession, user: User, token: str,
                        contacts: dict | None = None) -> Response:
    batch_id = await _new_batch(db, user)
    jobs = await _canonical_lead(db, user, batch_id=batch_id, contacts=contacts)
    await _batch(db, user, jobs, batch_id)
    return _ok(await client.get(f"/batches/{batch_id}/download", headers=_auth(token)))


async def test_segments_csv_is_byte_identical_for_canonical_contacts(
    mode, client: AsyncClient, db: AsyncSession, business_user: User, business_token: str,
):
    await _canonical_lead(db, business_user)
    assert (await _segments_csv(client, business_token)).content == GOLDEN_SEGMENTS_CSV


async def test_combined_csv_is_byte_identical_for_canonical_contacts(
    mode, client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
):
    resp = await _combined_csv(client, db, starter_user, starter_token)
    assert resp.content == GOLDEN_COMBINED_CSV


PADDED = {
    "phone": " 2065550100 ",
    "email": " owner@example.com ",
    "phones": [_phone(" 2065550100 ", " Mobile "), _phone(" 2065550101 ", "Landline")],
    "emails": [" owner@example.com ", " second@example.com "],
    "phone_type": " Mobile ",
}


async def test_padded_but_valid_contacts_are_trimmed_everywhere(
    mode, client: AsyncClient, db: AsyncSession, business_user: User, business_token: str,
):
    """The intentional normalization: a padded valid value is trimmed in every
    raw-SQL output (JSON and both CSVs)."""
    job_id = await _job(db, business_user, PROBATE)
    rid = await _lead(db, business_user, job_id, "PADDED", "WA|pierce|s2-pad", **PADDED)
    h = _auth(business_token)

    [row] = _ok(await client.post("/segments/union", json={"record_types": [PROBATE]},
                                  headers=h)).json()["rows"]
    assert row["id"] == rid
    assert (row["phone"], row["email"], row["phone_type"]) == ("2065550100", "owner@example.com", "Mobile")
    [cells] = _csv_rows(await _segments_csv(client, business_token))
    assert (cells["phone"], cells["phone_2"]) == ("2065550100", "2065550101")
    assert (cells["email"], cells["email_2"]) == ("owner@example.com", "second@example.com")

    resp = await _combined_csv(client, db, business_user, business_token, contacts=PADDED)
    cells = {r["party_name"]: r for r in _csv_rows(resp)}["GOLDEN OWNER"]
    assert (cells["email"], cells["email_2"]) == ("owner@example.com", "second@example.com")
    assert (cells["phone"], cells["phone_2"]) == ("2065550100", "2065550101")
