"""GET /batches/{id}/leads + /runs/{run_id}/leads — the in-app combined view.

DB-backed: the endpoints run the same combined SQL as the CSV on the async RLS
session, so tenant isolation, the ready-gate, pagination determinism, mode
filtering, and hidden-field blanking are all proven against real Postgres.
"""
import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

import src.utils.crypto as crypto
from src.config import settings
from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig
from tests.test_contact_decode import (
    CORRUPT_FE1,
    FERNET_SHAPED,
    _enc_json,
    _no_residue,
    _phone,
    _raw_set,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def overlap_batch(db, starter_user):
    """overlaps_only batch, done run, 1 overlap + 1 pk singleton + 1 no-parcel."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Leads",
        state="WA", fields=[], enrichment=[], schedule={}, deliver={},
        status="active", delivery_mode="overlaps_only",
    )
    db.add(batch)
    await db.flush()
    jobs = []
    for rt in ("probate", "tax_delinquent"):
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=starter_user.id, batch_id=batch.id,
            name=f"c-{rt}", county="pierce", state="WA", record_type=rt,
            fields=[], enrichment=[], schedule={}, deliver={},
        )
        db.add(cfg)
        await db.flush()
        job = Job(id=str(uuid.uuid4()), user_id=starter_user.id,
                  scraper_config_id=cfg.id, status="done", trigger="batch")
        db.add(job)
        await db.flush()
        jobs.append(job)
    for job, party, pk in (
        (jobs[0], "OVERLAP", "WA|pierce|0000000001"),
        (jobs[1], "OVERLAP", "WA|pierce|0000000001"),
        (jobs[0], "SINGLETON", "WA|pierce|0000000002"),
        (jobs[1], "NOPARCEL", None),
    ):
        db.add(Result(
            id=str(uuid.uuid4()), user_id=starter_user.id, job_id=job.id,
            date_recorded="06/01/2026", party_name=party, property_key=pk,
            # Leads carry an address (lead_actionability, 2026-09-02): rows with
            # neither property nor mailing address are quarantined from the
            # batch leads view, so every fixture row has one.
            property_address=f"1 {party} ST",
        ))
    run = BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
        status="done", child_job_ids=[j.id for j in jobs],
    )
    db.add(run)
    await db.commit()
    return batch, run


class TestBatchLeads:
    async def test_overlaps_only_page(self, client, starter_token, overlap_batch):
        batch, run = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(starter_token)
        )
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["delivery_mode"] == "overlaps_only"
        assert [lead["party_name"] for lead in body["leads"]] == ["OVERLAP"]
        assert body["leads"][0]["overlap_count"] == 2
        assert set(body["leads"][0]["matched_record_types"]) == {
            "probate", "tax_delinquent",
        }
        assert body["counts"] == {
            "leads_total": 3, "overlaps_delivered": 1,
            "singletons_suppressed": 1, "unmatchable_no_parcel": 1,
        }
        assert body["total"] == 1  # overlaps_only => total = overlaps

    async def test_run_scoped_variant(self, client, starter_token, overlap_batch):
        batch, run = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/runs/{run.id}/leads", headers=_auth(starter_token)
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_pagination_deterministic(self, client, starter_token, db,
                                            starter_user, overlap_batch):
        batch, run = overlap_batch
        # Flip mode to everything so 3 rows paginate.
        batch_row = await db.get(ScraperBatch, batch.id)
        batch_row.delivery_mode = "everything"
        await db.commit()
        p1 = await client.get(
            f"/batches/{batch.id}/leads?page=1&page_size=2",
            headers=_auth(starter_token),
        )
        p2 = await client.get(
            f"/batches/{batch.id}/leads?page=2&page_size=2",
            headers=_auth(starter_token),
        )
        names = [lead["party_name"] for lead in p1.json()["leads"]] + [
            lead["party_name"] for lead in p2.json()["leads"]
        ]
        assert len(names) == 3
        assert names[0] == "OVERLAP"  # overlap-first ordering
        assert len(set(names)) == 3  # no dup/missing rows across pages
        assert p1.json()["total"] == 3

    async def test_not_ready_while_running_404(self, client, starter_token, db,
                                               starter_user):
        batch = ScraperBatch(
            id=str(uuid.uuid4()), user_id=starter_user.id, name="R",
            state="WA", fields=[], enrichment=[], schedule={}, deliver={},
            status="active",
        )
        db.add(batch)
        await db.flush()
        db.add(BatchRun(
            id=str(uuid.uuid4()), batch_id=batch.id, user_id=starter_user.id,
            status="running", child_job_ids=[],
        ))
        await db.commit()
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(starter_token)
        )
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"  # Codex P2: 404s must not cache

    async def test_tenant_isolation(self, client, business_token, overlap_batch):
        batch, _ = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(business_token)
        )
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"  # Codex P2: 404s must not cache

    async def test_run_scoped_tenant_isolation(self, client, business_token, overlap_batch):
        batch, run = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/runs/{run.id}/leads", headers=_auth(business_token)
        )
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == "no-store"  # Codex P2: 404s must not cache


# ── New vs already delivered, contact provenance, quality, scoping (2026-10-04) ──
#
# The audit that drove these: a batch showed "1 lead on 2+ lists, 384 single-list"
# beside children reading "15 new" and "1 new". The combined set spans EVERY row of
# the child jobs, already-delivered ones included, and said nothing about which was
# which; the one contact on screen was the account's own earlier answer, reused free
# under skip tracing OFF, with nothing saying so.

async def _child(db, user_id, batch_id, record_type):
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, batch_id=batch_id,
        name=f"c-{record_type}", county="pierce", state="WA", record_type=record_type,
        fields=[], enrichment=[], schedule={}, deliver={},
    )
    db.add(cfg)
    await db.flush()
    job = Job(id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=cfg.id,
              status="done", trigger="batch")
    db.add(job)
    await db.flush()
    return job


async def _batch(db, user_id, name):
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=user_id, name=name, state="WA", fields=[],
        enrichment=[], schedule={}, deliver={}, status="active",
        delivery_mode="everything",
    )
    db.add(batch)
    await db.flush()
    return batch


@pytest_asyncio.fixture
async def audit_batch(db, starter_user, business_user):
    """Batch A (starter): a stacked already-delivered property whose probate row
    carries a REUSED contact, a new and an old single-list pre-foreclosure, and a new
    probate lead with no identity. Plus contamination bait for the SAME property as
    the new pre-foreclosure lead: a probate row in the same user's batch B, and a
    probate row of ANOTHER tenant whose job id is forged into A's child_job_ids.
    Either leaking in would turn that lead into a fake stacked one."""
    batch = await _batch(db, starter_user.id, "A")
    probate = await _child(db, starter_user.id, batch.id, "probate")
    prefc = await _child(db, starter_user.id, batch.id, "pre_foreclosure")

    def row(job, user_id, party, pk, **kw):
        db.add(Result(
            id=str(uuid.uuid4()), user_id=user_id, job_id=job.id,
            date_recorded="09/15/2026", party_name=party, property_key=pk,
            property_address=f"1 {party} ST", **kw,
        ))

    row(probate, starter_user.id, "STACKED", "pk-stacked", is_duplicate=True,
        parcel_id="0123456789", mailing_address="PO BOX 1",
        phone="2535550100", email="owner@example.com", skip_trace_status="hit",
        skip_trace_source="reused",
        skip_trace_attempted_at=datetime(2026, 9, 27, 5, 22, tzinfo=UTC))
    row(prefc, starter_user.id, "STACKED", "pk-stacked", is_duplicate=True,
        parcel_id="0123456789", auction_date=date(2026, 11, 6),
        default_amount=Decimal("1000.00"))
    row(prefc, starter_user.id, "NEW_PF", "pk-new", parcel_id="0222222222",
        mailing_address="PO BOX 2", auction_date=date(2026, 11, 13),
        default_amount=Decimal("2000.00"))
    row(prefc, starter_user.id, "OLD_PF", "pk-old", is_duplicate=True,
        parcel_id="0333333333")
    row(probate, starter_user.id, "NEW_PROB", None)

    other = await _batch(db, starter_user.id, "B")
    other_job = await _child(db, starter_user.id, other.id, "probate")
    row(other_job, starter_user.id, "BATCH_B", "pk-new", parcel_id="0222222222")

    foreign_batch = await _batch(db, business_user.id, "F")
    foreign_job = await _child(db, business_user.id, foreign_batch.id, "probate")
    row(foreign_job, business_user.id, "FOREIGN", "pk-new", parcel_id="0222222222",
        phone="2065550199", skip_trace_status="hit", skip_trace_source="lookup")

    for b, jobs in ((batch, [probate, prefc, foreign_job]), (other, [other_job])):
        db.add(BatchRun(id=str(uuid.uuid4()), batch_id=b.id, user_id=b.user_id,
                        status="done", child_job_ids=[j.id for j in jobs]))
    await db.commit()
    return batch


class TestBatchLeadsAudit:
    async def _get(self, client, token, batch, query=""):
        resp = await client.get(f"/batches/{batch.id}/leads?page_size=100{query}",
                                headers=_auth(token))
        assert resp.status_code == 200
        return resp.json()

    async def test_quality_counts_split_new_from_already_delivered(
        self, client, starter_token, audit_batch,
    ):
        body = await self._get(client, starter_token, audit_batch)
        assert body["quality"] == {
            "leads": 4, "new_leads": 2, "already_delivered": 2,
            "not_new_not_delivered": 0,
            "stacked": 1, "stacked_new": 0, "single_list": 2, "no_identity": 1,
            "with_parcel": 3, "with_property_address": 4, "with_mailing_address": 2,
            "with_phone": 1, "with_email": 1,
            "contacts_looked_up": 0, "contacts_reused": 1,
            # The stacked lead shows its probate row (contact first), so the file
            # carries no auction date for it: the check reports that, not a pass.
            "auction_applicable": 3, "with_auction_date": 1, "with_default_amount": 1,
            "tax_applicable": 0, "with_tax_balance": 0, "with_tax_year": 0,
        }
        # Exclusive groups reconcile.
        q = body["quality"]
        assert (q["new_leads"] + q["already_delivered"]
                + q["not_new_not_delivered"] == q["leads"])
        assert q["stacked"] + q["single_list"] + q["no_identity"] == q["leads"]
        assert body["total"] == q["leads"] == body["counts"]["leads_total"]

    async def test_rows_say_already_delivered_and_reused_contact(
        self, client, starter_token, audit_batch,
    ):
        leads = {r["party_name"]: r for r in (await self._get(
            client, starter_token, audit_batch))["leads"]}
        stacked = leads["STACKED"]
        assert stacked["already_delivered"] is True
        assert stacked["overlap_count"] == 2
        assert stacked["phone"] == "2535550100"
        assert stacked["contact_reused"] is True
        assert stacked["skip_trace_status"] == "hit"
        assert stacked["skip_trace_attempted_at"].startswith("2026-09-27")
        assert leads["NEW_PF"]["already_delivered"] is False
        assert leads["NEW_PF"]["contact_reused"] is False
        assert leads["NEW_PF"]["skip_trace_status"] == "not_attempted"
        assert leads["NEW_PROB"]["already_delivered"] is False

    async def test_delivery_filter_pages_over_its_own_total(
        self, client, starter_token, audit_batch,
    ):
        new = await self._get(client, starter_token, audit_batch, "&delivery=new")
        assert {r["party_name"] for r in new["leads"]} == {"NEW_PF", "NEW_PROB"}
        assert new["total"] == 2 and new["delivery"] == "new"
        old = await self._get(client, starter_token, audit_batch, "&delivery=delivered")
        assert {r["party_name"] for r in old["leads"]} == {"STACKED", "OLD_PF"}
        assert old["total"] == 2
        # Filters narrow the page, never the whole-set facts.
        assert new["quality"] == old["quality"]
        # The pager walks the filtered set: two pages of one, no row twice.
        pages = [
            (await client.get(
                f"/batches/{audit_batch.id}/leads?delivery=new&page_size=1&page={p}",
                headers=_auth(starter_token))).json()
            for p in (1, 2)
        ]
        assert [len(p["leads"]) for p in pages] == [1, 1]
        assert {p["leads"][0]["party_name"] for p in pages} == {"NEW_PF", "NEW_PROB"}

    async def test_delivery_filter_rejects_unknown_values(
        self, client, starter_token, audit_batch,
    ):
        resp = await client.get(f"/batches/{audit_batch.id}/leads?delivery=all",
                                headers=_auth(starter_token))
        assert resp.status_code == 422

    async def test_other_batch_and_other_tenant_cannot_contaminate(
        self, client, starter_token, audit_batch,
    ):
        body = await self._get(client, starter_token, audit_batch)
        names = {r["party_name"] for r in body["leads"]}
        assert "BATCH_B" not in names and "FOREIGN" not in names
        new_pf = next(r for r in body["leads"] if r["party_name"] == "NEW_PF")
        # Same property in batch B and in another tenant's (forged) job: neither may
        # make it a stacked lead or hand it a contact.
        assert new_pf["overlap_count"] == 1
        assert new_pf["matched_record_types"] == ["pre_foreclosure"]
        assert new_pf["phone"] is None
        assert body["quality"]["stacked"] == 1
        assert body["quality"]["contacts_looked_up"] == 0


# ─── UX 3.8a: phones / emails / contact_decode_failed on each combined lead ────
# The scalar phone / email were already decoded (3.8s2); the arrays now ride along
# through the same decoder, and a row says when a stored contact was unreadable,
# without that ever changing its skip_trace_status (the batch-audit field). Every
# case runs in BOTH PII_ENCRYPTION_STRICT modes; contacts are written through the
# ORM (stored encrypted, as production stores them) and malformed values with raw
# SQL bound to the row's id AND user (_raw_set asserts one row).

NOW = datetime.now(UTC)
FIVE_PHONES = [_phone(f"20655502{i:02d}", "Mobile") for i in range(5)]
FIVE_EMAILS = [f"e{i}@example.com" for i in range(5)]
VALID_PHONES = [_phone("2065550100"), _phone("2065550101", "Landline"), _phone("2065550102", None)]
VALID_EMAILS = ["owner@example.com", "second@example.com", "third@example.com"]


@pytest.fixture(params=["tolerant", "strict"])
def mode(request, monkeypatch):
    """Both read modes (the 3.8s1 fixture): the key and blind index are built in
    tolerant mode first, because strict refuses the test SECRET_KEY-derived key."""
    crypto._instance()
    crypto._blind_index_secret()
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", request.param == "strict")
    return request.param


@pytest_asyncio.fixture
async def contact_batch(db, starter_user):
    """One 'everything' batch, each lead its own property unless named a bucket.
    Jobs: J1 newest, J2 older, J3 oldest (the ranking's recency tie-break)."""
    uid = starter_user.id
    batch = await _batch(db, uid, "Contacts")
    jobs = {}
    for name, rt, age in (("J1", "probate", 1), ("J2", "probate", 3), ("J3", "pre_foreclosure", 5)):
        job = await _child(db, uid, batch.id, rt)
        job.created_at = NOW - timedelta(days=age)
        jobs[name] = job
    await db.flush()
    ids = {}

    def row(key, job, party, pk, status="not_attempted", **contacts):
        rid = str(uuid.uuid4())
        ids[key] = rid
        db.add(Result(
            id=rid, user_id=uid, job_id=jobs[job].id, date_recorded="09/15/2026",
            party_name=party, property_key=pk, property_address=f"1 {party} ST",
            skip_trace_status=status, **contacts,
        ))

    row("HIT", "J1", "HIT", "pk-hit", "hit", phone="2065550100", email="owner@example.com",
        phone_type="Mobile", phones=VALID_PHONES, emails=VALID_EMAILS)
    row("MANY", "J1", "MANY", "pk-many", "hit", phone="2065550200", email="e0@example.com",
        phones=FIVE_PHONES, emails=FIVE_EMAILS)
    row("PURGED", "J1", "PURGED", "pk-purged", "purged")
    row("MISS", "J1", "MISS", "pk-miss", "miss", phones=[], emails=[])
    row("ERRORED", "J1", "ERRORED", "pk-errored", "errored")
    row("LEGACY", "J1", "LEGACY", "pk-legacy")
    row("RESIDUE", "J1", "RESIDUE", "pk-residue", "hit", phone="2065550180",
        email="gone@example.com", phones=VALID_PHONES, emails=VALID_EMAILS)
    # Mixed bucket: the hit row is the OLDEST, so recency alone would pick another.
    row("MIX_QUEUED", "J1", "MIXED", "pk-mixed", "queued")
    row("MIX_ERRORED", "J2", "MIXED", "pk-mixed", "errored")
    row("MIX_HIT", "J3", "MIXED", "pk-mixed", "hit", phone="2065550160")
    # Scalar-primary: A (newer) has arrays but NULL scalars, B a scalar phone.
    row("SP_A", "J1", "SCALAR PRIMARY", "pk-sp", "hit", phones=[_phone("2065550165")],
        emails=["a@example.com"])
    row("SP_B", "J2", "SCALAR PRIMARY", "pk-sp", "hit", phone="2065550166")
    # A bucket whose only row is arrays-only.
    row("AO", "J1", "ARRAYS ONLY", "pk-ao", "hit", phones=[_phone("2065550167")],
        emails=["ao@example.com"])
    # Corrupt representative: A (newer) malformed scalar phone, B (older) valid.
    row("CR_A", "J1", "CORRUPT REP", "pk-cr", "hit", phone="2065550170")
    row("CR_B", "J2", "CORRUPT REP", "pk-cr", "hit", phone="2065550171")

    run = BatchRun(id=str(uuid.uuid4()), batch_id=batch.id, user_id=uid, status="done",
                   child_job_ids=[j.id for j in jobs.values()])
    db.add(run)
    await db.commit()
    _raw_set(ids["LEGACY"], uid, phone="2065550144")
    _raw_set(ids["RESIDUE"], uid, phone=CORRUPT_FE1,
             phones=_enc_json([_phone(CORRUPT_FE1), _phone("2065550188", "fe1:zz")]),
             emails=_enc_json([FERNET_SHAPED]))
    _raw_set(ids["CR_A"], uid, phone=CORRUPT_FE1)
    return batch.id, run.id, ids


class TestBatchLeadContacts:
    async def _pages(self, client, token, batch_id, run_id):
        out = []
        for path in (f"/batches/{batch_id}/leads", f"/batches/{batch_id}/runs/{run_id}/leads"):
            resp = await client.get(path, params={"page_size": 100}, headers=_auth(token))
            assert resp.status_code == 200, (path, resp.text[:300])
            _no_residue(resp.text)
            assert resp.headers["cache-control"] == "no-store"
            out.append(resp)
        return out

    async def test_contacts_decode_and_flag_without_touching_status(
        self, mode, client, starter_token, contact_batch,
    ):
        batch_id, run_id, ids = contact_batch
        tolerant = mode == "tolerant"
        for resp in await self._pages(client, starter_token, batch_id, run_id):
            leads = {r["party_name"]: r for r in resp.json()["leads"]}

            hit = leads["HIT"]
            assert (hit["phone"], hit["email"], hit["skip_trace_status"]) == (
                "2065550100", "owner@example.com", "hit")
            assert hit["phones"] == VALID_PHONES and hit["emails"] == VALID_EMAILS
            assert hit["contact_decode_failed"] is False

            many = leads["MANY"]
            assert many["phones"] == FIVE_PHONES[:3] and many["emails"] == FIVE_EMAILS[:3]

            purged = leads["PURGED"]
            assert (purged["phones"], purged["emails"], purged["skip_trace_status"]) == (None, None, "purged")
            miss = leads["MISS"]
            assert (miss["phones"], miss["emails"], miss["skip_trace_status"]) == ([], [], "miss")
            assert leads["ERRORED"]["skip_trace_status"] == "errored"

            legacy = leads["LEGACY"]
            assert legacy["phone"] == ("2065550144" if tolerant else None)
            assert legacy["phones"] is None and legacy["skip_trace_status"] == "not_attempted"
            assert legacy["contact_decode_failed"] is (not tolerant)

            residue = leads["RESIDUE"]
            assert (residue["phone"], residue["emails"]) == (None, None)
            assert residue["phones"] == [_phone("2065550188", None)]
            assert residue["email"] == "gone@example.com"
            assert residue["contact_decode_failed"] is True
            assert residue["skip_trace_status"] == "hit"  # a decode failure is not a lookup result

    async def test_representatives_are_unchanged_by_decoding(
        self, mode, client, starter_token, contact_batch,
    ):
        """The ranking runs in SQL over the stored scalars and 3.8a changes no SQL:
        mixed bucket -> the hit row; scalar-primary -> B; arrays-only bucket -> its
        row with arrays decoded; a malformed scalar still wins its bucket and reads
        absent, the sibling's valid phone not substituted (accepted, stated)."""
        batch_id, run_id, ids = contact_batch
        for resp in await self._pages(client, starter_token, batch_id, run_id):
            leads = {r["party_name"]: r for r in resp.json()["leads"]}
            mixed = leads["MIXED"]
            assert (mixed["id"], mixed["phone"], mixed["skip_trace_status"]) == (
                ids["MIX_HIT"], "2065550160", "hit")
            sp = leads["SCALAR PRIMARY"]
            assert (sp["id"], sp["phone"], sp["phones"]) == (ids["SP_B"], "2065550166", None)
            ao = leads["ARRAYS ONLY"]
            assert (ao["id"], ao["phone"]) == (ids["AO"], None)
            assert ao["phones"] == [_phone("2065550167")] and ao["emails"] == ["ao@example.com"]
            cr = leads["CORRUPT REP"]
            assert (cr["id"], cr["phone"], cr["contact_decode_failed"]) == (ids["CR_A"], None, True)
            assert "2065550171" not in resp.text

    async def test_page_order_matches_the_combined_csv(
        self, mode, client, starter_token, contact_batch,
    ):
        """Same SQL, same order: the JSON page lists the leads in exactly the order
        the combined CSV writes them, and the CSV carries no residue either."""
        batch_id, run_id, _ = contact_batch
        latest, _scoped = await self._pages(client, starter_token, batch_id, run_id)
        csv_resp = await client.get(f"/batches/{batch_id}/download", headers=_auth(starter_token))
        assert csv_resp.status_code == 200
        _no_residue(csv_resp.text)
        rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
        assert [r["party_name"] for r in rows] == [r["party_name"] for r in latest.json()["leads"]]
        cells = {r["party_name"]: r for r in rows}
        assert cells["CORRUPT REP"]["phone"] == ""
        assert (cells["HIT"]["phone_2"], cells["HIT"]["email_3"]) == ("2065550101", "third@example.com")
