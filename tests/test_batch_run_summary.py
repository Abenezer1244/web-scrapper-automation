"""One batch = one run: the list/detail facts a dashboard row needs (2026-10-04).

The dashboard showed a two-scrape batch as two unrelated scraper rows. The fix uses
the real parent link (scraper_configs.batch_id, batch_runs.child_job_ids), never
names, and these facts come from what the latest run's children actually did:
done/failed counts, new records billed, skip tracing as configured, and lookups as
executed. DB-backed, real Postgres.
"""
import uuid

from sqlalchemy import select

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_batch(db, user_id, name, children, *, extra_job_ids=()):
    """children: (county, record_type, job_status, record_count, skip_on, rows)
    where rows = list of dicts of Result overrides. Returns the batch id."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=user_id, name=name, state="WA", fields=[],
        enrichment=[], schedule={}, deliver={}, status="active",
    )
    db.add(batch)
    await db.flush()
    job_ids = []
    for county, rt, st, rc, skip_on, rows in children:
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=user_id, batch_id=batch.id,
            name=f"{name} - {county} {rt}", county=county, state="WA",
            record_type=rt, fields=[], enrichment=[], schedule={}, deliver={},
            skip_trace_enabled=skip_on,
        )
        db.add(cfg)
        await db.flush()
        job = Job(id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=cfg.id,
                  status=st, trigger="batch", record_count=rc)
        db.add(job)
        await db.flush()
        job_ids.append(job.id)
        for i, over in enumerate(rows):
            db.add(Result(
                id=str(uuid.uuid4()), user_id=user_id, job_id=job.id,
                date_recorded="09/15/2026", party_name=f"P{i}",
                property_address=f"{i} MAIN ST", **over,
            ))
    db.add(BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=user_id,
        status="done" if all(c[2] == "done" for c in children) else "running",
        child_job_ids=job_ids + list(extra_job_ids),
    ))
    await db.commit()
    return batch.id, job_ids


async def _list(client, token):
    resp = await client.get("/batches", headers=_auth(token))
    assert resp.status_code == 200
    return {b["name"]: b for b in resp.json()}


class TestOneRunPerBatch:
    async def test_two_child_batch_is_one_row_with_rollups(
        self, client, db, starter_user, starter_token,
    ):
        await _make_batch(db, starter_user.id, "testas", [
            ("pierce", "probate", "done", 1, False, []),
            ("pierce", "pre_foreclosure", "done", 15, False, []),
        ])
        rows = await _list(client, starter_token)
        assert list(rows) == ["testas"]  # ONE row, not one per child
        b = rows["testas"]
        assert b["child_count"] == 2
        assert b["children_done"] == 2 and b["children_failed"] == 0
        assert b["new_records"] == 16
        assert b["skip_trace"] == "off"
        assert b["contacts_looked_up"] == 0
        assert b["counties"] == ["pierce"]
        assert b["record_types"] == ["pre_foreclosure", "probate"]

    async def test_ten_child_multi_county_batch_is_one_row(
        self, client, db, starter_user, starter_token,
    ):
        counties = ["king", "pierce", "snohomish", "clark", "king"] * 2
        types = ["probate", "pre_foreclosure", "tax_delinquent", "trustee_sale",
                 "code_violation"] * 2
        await _make_batch(db, starter_user.id, "wide", [
            (c, t, "done", 1, False, []) for c, t in zip(counties, types, strict=True)
        ])
        b = (await _list(client, starter_token))["wide"]
        assert b["child_count"] == 10
        assert b["children_done"] == 10
        assert b["counties"] == ["clark", "king", "pierce", "snohomish"]
        assert len(b["record_types"]) == 5

    async def test_running_and_failed_children_roll_up_honestly(
        self, client, db, starter_user, starter_token,
    ):
        await _make_batch(db, starter_user.id, "mixed-status", [
            ("pierce", "probate", "done", 4, False, []),
            ("pierce", "pre_foreclosure", "scraping", 9, False, []),
            ("king", "probate", "failed", 7, False, []),
        ])
        b = (await _list(client, starter_token))["mixed-status"]
        assert b["run_status"] == "running"
        assert b["children_done"] == 1
        assert b["children_failed"] == 1
        # Only the done child's billed records: an in-flight counter and a failed
        # child's records are not new leads anyone can download.
        assert b["new_records"] == 4

    async def test_skip_trace_rollup_off_on_mixed(
        self, client, db, starter_user, starter_token,
    ):
        await _make_batch(db, starter_user.id, "on", [
            ("pierce", "probate", "done", 1, True, []),
        ])
        await _make_batch(db, starter_user.id, "mixed", [
            ("pierce", "probate", "done", 1, True, []),
            ("pierce", "pre_foreclosure", "done", 1, False, []),
        ])
        rows = await _list(client, starter_token)
        assert rows["on"]["skip_trace"] == "on"
        assert rows["mixed"]["skip_trace"] == "mixed"

    async def test_lookups_are_counted_from_execution_not_reuse(
        self, client, db, starter_user, starter_token,
    ):
        """A batch configured OFF still reports a lookup bought for one of its
        rows (never labelled clean), and a reused answer is not a lookup."""
        await _make_batch(db, starter_user.id, "off-but-looked-up", [
            ("pierce", "probate", "done", 2, False, [
                {"skip_trace_status": "hit", "skip_trace_source": "lookup"},
                {"skip_trace_status": "hit", "skip_trace_source": "reused"},
            ]),
        ])
        b = (await _list(client, starter_token))["off-but-looked-up"]
        assert b["skip_trace"] == "off"
        assert b["contacts_looked_up"] == 1


class TestScoping:
    async def test_another_batch_cannot_contaminate_the_row(
        self, client, db, starter_user, starter_token,
    ):
        await _make_batch(db, starter_user.id, "A", [
            ("pierce", "probate", "done", 3, False, []),
        ])
        await _make_batch(db, starter_user.id, "B", [
            ("pierce", "probate", "done", 50, True, [
                {"skip_trace_status": "hit", "skip_trace_source": "lookup"},
            ]),
        ])
        rows = await _list(client, starter_token)
        assert rows["A"]["new_records"] == 3
        assert rows["A"]["contacts_looked_up"] == 0
        assert rows["B"]["new_records"] == 50

    async def test_a_forged_foreign_job_id_counts_for_nothing(
        self, client, db, starter_user, starter_token, business_user,
    ):
        """Another tenant's job id inside this run's child_job_ids (it cannot get
        there through the API, but the read must not trust it either) adds no
        records and no lookups."""
        _, foreign = await _make_batch(db, business_user.id, "theirs", [
            ("pierce", "probate", "done", 99, True, [
                {"skip_trace_status": "hit", "skip_trace_source": "lookup"},
            ]),
        ])
        await _make_batch(db, starter_user.id, "mine", [
            ("pierce", "probate", "done", 2, False, []),
        ], extra_job_ids=foreign)
        rows = await _list(client, starter_token)
        assert "theirs" not in rows
        assert rows["mine"]["new_records"] == 2
        assert rows["mine"]["children_done"] == 1
        assert rows["mine"]["contacts_looked_up"] == 0

    async def test_a_same_user_other_batch_job_id_counts_for_nothing(
        self, client, db, starter_user, starter_token,
    ):
        """Codex P1: user scoping alone would let batch B's job, wrongly listed in
        A's run, count for A. A job counts only for its own config's batch."""
        _, b_jobs = await _make_batch(db, starter_user.id, "B", [
            ("pierce", "probate", "done", 40, True, [
                {"skip_trace_status": "hit", "skip_trace_source": "lookup"},
            ]),
        ])
        await _make_batch(db, starter_user.id, "A", [
            ("pierce", "probate", "done", 2, False, []),
        ], extra_job_ids=b_jobs)
        rows = await _list(client, starter_token)
        assert rows["A"]["new_records"] == 2
        assert rows["A"]["children_done"] == 1
        assert rows["A"]["contacts_looked_up"] == 0
        assert rows["B"]["new_records"] == 40

    async def test_a_repeated_child_id_is_counted_once(
        self, client, db, starter_user, starter_token,
    ):
        batch_id, jobs = await _make_batch(db, starter_user.id, "dupe", [
            ("pierce", "probate", "done", 5, False, []),
        ])
        run = (await db.execute(
            select(BatchRun).where(BatchRun.batch_id == batch_id)
        )).scalar_one()
        run.child_job_ids = jobs + jobs
        await db.commit()
        b = (await _list(client, starter_token))["dupe"]
        assert b["children_done"] == 1
        assert b["new_records"] == 5


class TestDetailChildren:
    async def test_children_carry_already_delivered_and_skip_setting(
        self, client, db, starter_user, starter_token,
    ):
        batch_id, _ = await _make_batch(db, starter_user.id, "detail", [
            ("pierce", "probate", "done", 1, False, [
                {"is_duplicate": False},
                {"is_duplicate": True, "duplicate_reason": "prior_run"},
                {"is_duplicate": True, "duplicate_reason": "prior_run"},
                # A same-run sibling is not something an earlier run delivered.
                {"is_duplicate": True, "duplicate_reason": "same_run"},
            ]),
            ("pierce", "pre_foreclosure", "failed", 0, True, [
                {"is_duplicate": True},
            ]),
        ])
        resp = await client.get(f"/batches/{batch_id}", headers=_auth(starter_token))
        assert resp.status_code == 200
        body = resp.json()
        kids = {c["record_type"]: c for c in body["children"]}
        assert kids["probate"]["record_count"] == 1
        assert kids["probate"]["already_delivered_count"] == 2
        assert kids["probate"]["skip_trace_enabled"] is False
        # A failed child delivered nothing: no new and no already-delivered figure.
        assert kids["pre_foreclosure"]["already_delivered_count"] == 0
        assert kids["pre_foreclosure"]["skip_trace_enabled"] is True
        assert body["skip_trace"] == "mixed"
        assert body["children_done"] == 1 and body["children_failed"] == 1
