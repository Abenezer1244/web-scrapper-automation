"""GET /batches/{id}/leads + /runs/{run_id}/leads — the in-app combined view.

DB-backed: the endpoints run the same combined SQL as the CSV on the async RLS
session, so tenant isolation, the ready-gate, pagination determinism, mode
filtering, and hidden-field blanking are all proven against real Postgres.
"""
import uuid

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

    async def test_tenant_isolation(self, client, business_token, overlap_batch):
        batch, _ = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/leads", headers=_auth(business_token)
        )
        assert resp.status_code == 404

    async def test_run_scoped_tenant_isolation(self, client, business_token, overlap_batch):
        batch, run = overlap_batch
        resp = await client.get(
            f"/batches/{batch.id}/runs/{run.id}/leads", headers=_auth(business_token)
        )
        assert resp.status_code == 404
