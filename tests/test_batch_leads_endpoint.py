"""GET /batches/{id}/leads + /runs/{run_id}/leads — the in-app combined view.

DB-backed: the endpoints run the same combined SQL as the CSV on the async RLS
session, so tenant isolation, the ready-gate, pagination determinism, mode
filtering, and hidden-field blanking are all proven against real Postgres.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest_asyncio

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig


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
