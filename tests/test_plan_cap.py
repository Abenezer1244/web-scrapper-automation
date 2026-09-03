"""The plan quota cap (src/workers/tasks_helpers/plan_cap.py) against a real DB.

This module exists because the cap's first implementation shipped three defects
that a fully green 2000-test suite could not see: nothing could reach the cap
without running an entire scrape. Each class below pins one of them.

The invariant under test throughout: the rows `is_actionable()` accepts in
Python are exactly the rows `actionable_sql` counts in the database. Display,
export, counting and billing all read one of those two spellings, so the moment
they disagree the user is shown one number and charged another.
"""
import uuid

import pytest
from sqlalchemy import select, text

from src.api.auth import hash_password
from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    OVER_QUOTA,
    actionable_sql,
    is_actionable,
)
from src.db import session as _db_session
from src.db.models import DeliveredRecord, Job, Result, ScraperConfig, User
from src.workers.tasks_helpers.plan_cap import apply_plan_cap

pytestmark = pytest.mark.asyncio


async def _seed(n_actionable: int, *, n_unactionable: int = 0,
                n_duplicate: int = 0, with_hashes: bool = False):
    """A user + config + done job with a mix of rows. Returns (ids, job_id, user_id)."""
    uid, cid, jid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(User(id=uid, email=f"cap-{uid[:8]}@example.com",
                   password_hash=hash_password("TestPass123!"),
                   plan="pro", records_limit=1000, records_used=0))
        s.add(ScraperConfig(id=cid, user_id=uid, name="cap", county="pierce",
                            state="WA", record_type="probate"))
        s.add(Job(id=jid, user_id=uid, scraper_config_id=cid, status="done",
                  trigger="manual"))
        await s.commit()

    ids = {"actionable": [], "unactionable": [], "duplicate": []}
    async with _db_session.AsyncSessionLocal() as s:
        for i in range(n_actionable):
            rid = str(uuid.uuid4())
            ids["actionable"].append(rid)
            s.add(Result(
                id=rid, job_id=jid, user_id=uid,
                # party_name drives the canonical ORDER BY, so make it explicit.
                party_name=f"OWNER {i:03d}",
                property_address=f"{i} MAIN ST",
                dedup_hash=f"hash-{rid}" if with_hashes else None,
                is_duplicate=False,
            ))
        for i in range(n_unactionable):
            rid = str(uuid.uuid4())
            ids["unactionable"].append(rid)
            s.add(Result(id=rid, job_id=jid, user_id=uid,
                         party_name=f"NOADDR {i:03d}",
                         property_address=None, mailing_address=None,
                         is_duplicate=False))
        for i in range(n_duplicate):
            rid = str(uuid.uuid4())
            ids["duplicate"].append(rid)
            s.add(Result(id=rid, job_id=jid, user_id=uid,
                         party_name=f"DUPE {i:03d}",
                         property_address=f"{i} DUPE ST", is_duplicate=True))
        await s.commit()
    return ids, jid, uid


def _sync():
    return _db_session.SyncSessionLocal()


def _db_actionable_ids(db, job_id: str, user_id: str) -> set[str]:
    """What the DATABASE thinks is deliverable — the billing spelling."""
    rows = db.execute(
        text(
            "SELECT id FROM results WHERE job_id = :jid "
            "AND user_id = CAST(:uid AS uuid) AND is_duplicate = false "
            f"AND {actionable_sql('results')}"
        ),
        {"jid": job_id, "uid": user_id},
    ).fetchall()
    return {str(r[0]) for r in rows}


class TestCapSelectsTheRightRows:
    async def test_marks_exactly_the_overflow(self):
        ids, jid, uid = await _seed(10)
        with _sync() as db:
            capped = apply_plan_cap(db, jid, uid, remaining=4)
            assert len(capped) == 6
            assert len(_db_actionable_ids(db, jid, uid)) == 4

    async def test_quota_at_or_above_supply_marks_nothing(self):
        ids, jid, uid = await _seed(3)
        with _sync() as db:
            assert apply_plan_cap(db, jid, uid, remaining=10) == []
            assert len(_db_actionable_ids(db, jid, uid)) == 3

    async def test_zero_quota_marks_everything(self):
        ids, jid, uid = await _seed(3)
        with _sync() as db:
            assert len(apply_plan_cap(db, jid, uid, remaining=0)) == 3
            assert _db_actionable_ids(db, jid, uid) == set()

    async def test_unactionable_rows_never_consume_quota(self):
        """The whole point of the change: addressless rows must not eat the
        quota and crowd out real leads, which is what the old raw slice did."""
        ids, jid, uid = await _seed(5, n_unactionable=20)
        with _sync() as db:
            apply_plan_cap(db, jid, uid, remaining=5)
            # All five real leads survive despite 20 addressless rows present.
            assert len(_db_actionable_ids(db, jid, uid)) == 5

    async def test_duplicates_are_not_ranked(self):
        ids, jid, uid = await _seed(4, n_duplicate=10)
        with _sync() as db:
            capped = apply_plan_cap(db, jid, uid, remaining=4)
            assert capped == []  # only the 4 non-duplicates were candidates

    async def test_ranking_is_the_canonical_order(self):
        """Bill, file and screen agree only if the cap keeps the same rows the
        export orders by (party_name, date_recorded, id)."""
        ids, jid, uid = await _seed(6)
        with _sync() as db:
            apply_plan_cap(db, jid, uid, remaining=2)
            kept = _db_actionable_ids(db, jid, uid)
            names = db.execute(
                select(Result.party_name).where(Result.id.in_(kept))
            ).scalars().all()
            assert sorted(names) == ["OWNER 000", "OWNER 001"]


class TestStaleIdentityMap:
    """The defect that shipped: sessions use expire_on_commit=False, so Result
    objects loaded BEFORE the cap keep their stale enrichment_data. The export
    reads those objects with is_actionable() while billing reads the DB — so
    without populate_existing the file and the bill disagree."""

    async def test_reload_without_populate_existing_is_stale(self):
        ids, jid, uid = await _seed(6)
        with _sync() as db:
            preloaded = db.execute(
                select(Result).where(Result.job_id == jid)
            ).scalars().all()
            assert len(preloaded) == 6

            apply_plan_cap(db, jid, uid, remaining=2)

            # A plain re-SELECT returns the SAME identities, still unmarked.
            plain = db.execute(
                select(Result).where(Result.job_id == jid)
            ).scalars().all()
            stale_view = {r.id for r in plain if is_actionable(r)}

            fresh = db.execute(
                select(Result).where(Result.job_id == jid)
                .execution_options(populate_existing=True)
            ).scalars().all()
            fresh_view = {r.id for r in fresh if is_actionable(r)}

            db_view = _db_actionable_ids(db, jid, uid)

            # populate_existing agrees with the database; the plain reload does not.
            assert fresh_view == db_view
            assert len(fresh_view) == 2
            assert stale_view != db_view, (
                "expected the plain reload to be stale — if this ever passes, "
                "expire_on_commit changed and the populate_existing guard in "
                "run_scrape_job may no longer be load-bearing"
            )


class TestNonObjectEnrichmentData:
    """`jsonb || jsonb_build_object(...)` on an array or scalar produces no
    top-level object, so `->>` could not read the marker back: the row was
    RETURNED as capped while staying actionable, exportable and billable."""

    @pytest.mark.parametrize("weird", ['[1,2,3]', '"a string"', '42', 'null'])
    async def test_marker_survives_non_object_enrichment_data(self, weird):
        ids, jid, uid = await _seed(3)
        with _sync() as db:
            db.execute(
                text("UPDATE results SET enrichment_data = CAST(:v AS json) "
                     "WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"v": weird, "ids": ids["actionable"]},
            )
            db.commit()
            capped = apply_plan_cap(db, jid, uid, remaining=0)
            assert len(capped) == 3
            # Reported capped AND actually excluded — the two must agree.
            assert _db_actionable_ids(db, jid, uid) == set()

    async def test_object_enrichment_data_keeps_its_other_keys(self):
        ids, jid, uid = await _seed(2)
        with _sync() as db:
            db.execute(
                text("UPDATE results SET enrichment_data = "
                     """CAST('{"assessed_value": 400000}' AS json) """
                     "WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ids["actionable"]},
            )
            db.commit()
            apply_plan_cap(db, jid, uid, remaining=0)
            kept = db.execute(
                text("SELECT enrichment_data->>'assessed_value' FROM results "
                     "WHERE id = :rid"),
                {"rid": ids["actionable"][0]},
            ).scalar()
            assert kept == "400000"


class TestIdempotency:
    async def test_rerun_marks_the_same_set(self):
        """A watchdog re-run must not renumber the survivors and mark a SECOND
        batch — that would shrink the delivered set on every pass."""
        ids, jid, uid = await _seed(10)
        with _sync() as db:
            first = set(apply_plan_cap(db, jid, uid, remaining=4))
            after_first = _db_actionable_ids(db, jid, uid)
            second = set(apply_plan_cap(db, jid, uid, remaining=4))
            after_second = _db_actionable_ids(db, jid, uid)
            assert first == second
            assert after_first == after_second
            assert len(after_second) == 4

    async def test_a_raised_quota_gives_the_leads_back(self):
        ids, jid, uid = await _seed(10)
        with _sync() as db:
            apply_plan_cap(db, jid, uid, remaining=2)
            assert len(_db_actionable_ids(db, jid, uid)) == 2
            # Upgraded plan / new month: the clear step must restore them.
            apply_plan_cap(db, jid, uid, remaining=8)
            assert len(_db_actionable_ids(db, jid, uid)) == 8


class TestDedupClaimRelease:
    async def test_capped_rows_release_their_claim(self):
        """A capped lead was never delivered or billed, so its dedup claim must
        go — otherwise it is permanently unreachable instead of deferred."""
        ids, jid, uid = await _seed(4, with_hashes=True)
        async with _db_session.AsyncSessionLocal() as s:
            for rid in ids["actionable"]:
                s.add(DeliveredRecord(user_id=uid, dedup_hash=f"hash-{rid}",
                                      first_job_id=jid))
            await s.commit()

        with _sync() as db:
            capped = apply_plan_cap(db, jid, uid, remaining=1)
            assert len(capped) == 3
            left = db.execute(
                text("SELECT count(*) FROM delivered_records "
                     "WHERE user_id = CAST(:uid AS uuid)"),
                {"uid": uid},
            ).scalar()
            assert left == 1  # only the delivered one keeps its claim

    async def test_another_jobs_claim_is_never_released(self):
        """The release matched on (user_id, dedup_hash) with no ownership guard,
        so it could delete a DIFFERENT job's durable claim for the same user —
        letting an already-delivered lead be delivered and billed twice."""
        ids, jid, uid = await _seed(2, with_hashes=True)
        other_job = str(uuid.uuid4())
        async with _db_session.AsyncSessionLocal() as s:
            cfg = (await s.execute(select(ScraperConfig).where(
                ScraperConfig.user_id == uid))).scalars().first()
            s.add(Job(id=other_job, user_id=uid, scraper_config_id=cfg.id,
                      status="done", trigger="manual"))
            await s.commit()
        async with _db_session.AsyncSessionLocal() as s:
            # An EARLIER job legitimately delivered the same hash.
            for rid in ids["actionable"]:
                s.add(DeliveredRecord(user_id=uid, dedup_hash=f"hash-{rid}",
                                      first_job_id=other_job))
            await s.commit()

        with _sync() as db:
            apply_plan_cap(db, jid, uid, remaining=0)
            survived = db.execute(
                text("SELECT count(*) FROM delivered_records "
                     "WHERE user_id = CAST(:uid AS uuid) AND first_job_id = :oj"),
                {"uid": uid, "oj": other_job},
            ).scalar()
            assert survived == 2, "the other job's claims must be untouched"


class TestTenantIsolation:
    async def test_cap_never_touches_another_tenants_rows(self):
        ids_a, jid_a, uid_a = await _seed(4)
        ids_b, jid_b, uid_b = await _seed(4)
        with _sync() as db:
            apply_plan_cap(db, jid_a, uid_a, remaining=0)
            assert _db_actionable_ids(db, jid_a, uid_a) == set()
            # Tenant B is entirely unaffected.
            assert len(_db_actionable_ids(db, jid_b, uid_b)) == 4

    async def test_marker_constants_are_what_the_rule_reads(self):
        # Guards against the marker and the rule drifting apart.
        assert DELIVERY_EXCLUDED_KEY in actionable_sql("r")
        assert OVER_QUOTA in actionable_sql("r")


class TestCostGuardWithdrawsSkipTrace:
    """`_enqueue_skip_trace_rows` runs INSIDE inline enrichment, i.e. BEFORE the
    cap exists, so a finite-quota user's over-quota rows can already be queued
    for a PAID Tracerfy lookup by the time we mark them."""

    async def _queue_for(self, jid, uid, result_ids, status="queued"):
        from src.db.models import PendingSkipTraceRow
        async with _db_session.AsyncSessionLocal() as s:
            for rid in result_ids:
                s.add(PendingSkipTraceRow(
                    job_id=jid, result_id=rid, user_id=uid,
                    property_address="1 MAIN ST", trace_type="advanced",
                    status=status,
                ))
            await s.commit()

    async def _pending_count(self, db, uid, status=None):
        q = ("SELECT count(*) FROM pending_skip_trace_rows "
             "WHERE user_id = CAST(:uid AS uuid)")
        p = {"uid": uid}
        if status:
            q += " AND status = :st"
            p["st"] = status
        return db.execute(text(q), p).scalar()

    async def test_queued_rows_for_capped_leads_are_withdrawn(self):
        ids, jid, uid = await _seed(6)
        await self._queue_for(jid, uid, ids["actionable"])
        with _sync() as db:
            assert await self._pending_count(db, uid) == 6
            capped = apply_plan_cap(db, jid, uid, remaining=2)
            assert len(capped) == 4
            # Only the two we will actually deliver keep their paid lookup.
            assert await self._pending_count(db, uid) == 2

    async def test_submitting_rows_are_never_withdrawn(self):
        """A 'submitting' row has already been POSTed and may have been charged.
        The dispatcher's standing rule is that such a row is never auto-resolved
        — releasing it is exactly how you pay twice."""
        ids, jid, uid = await _seed(4)
        await self._queue_for(jid, uid, ids["actionable"], status="submitting")
        with _sync() as db:
            apply_plan_cap(db, jid, uid, remaining=0)
            assert await self._pending_count(db, uid, "submitting") == 4

    async def test_another_tenants_queued_rows_are_untouched(self):
        ids_a, jid_a, uid_a = await _seed(3)
        ids_b, jid_b, uid_b = await _seed(3)
        await self._queue_for(jid_a, uid_a, ids_a["actionable"])
        await self._queue_for(jid_b, uid_b, ids_b["actionable"])
        with _sync() as db:
            apply_plan_cap(db, jid_a, uid_a, remaining=0)
            assert await self._pending_count(db, uid_a) == 0
            assert await self._pending_count(db, uid_b) == 3


class TestWithdrawnRowsAreNotStranded:
    """Deleting the work row without resetting the lead's own status left it
    'queued' with nothing left to process it, so a later cap-clear (upgrade, new
    month, re-run) brought it back visible and permanently "Processing…"."""

    async def test_status_is_reset_when_the_pending_row_is_withdrawn(self):
        from src.db.models import PendingSkipTraceRow
        ids, jid, uid = await _seed(4)
        async with _db_session.AsyncSessionLocal() as s:
            for rid in ids["actionable"]:
                s.add(PendingSkipTraceRow(
                    job_id=jid, result_id=rid, user_id=uid,
                    property_address="1 MAIN ST", trace_type="advanced",
                    status="queued",
                ))
            await s.commit()
        async with _db_session.AsyncSessionLocal() as s:
            await s.execute(text(
                "UPDATE results SET skip_trace_status = 'queued' "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids["actionable"]})
            await s.commit()

        with _sync() as db:
            apply_plan_cap(db, jid, uid, remaining=1)
            stranded = db.execute(text(
                "SELECT count(*) FROM results WHERE job_id = :jid "
                "AND skip_trace_status = 'queued' AND NOT EXISTS ("
                "  SELECT 1 FROM pending_skip_trace_rows p "
                "  WHERE p.result_id = results.id)"),
                {"jid": jid}).scalar()
            assert stranded == 0, "a withdrawn lookup must not leave the lead queued"
