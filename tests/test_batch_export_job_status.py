"""A batch child that failed delivered nothing, so its rows stay out of the CSV.

`finalize_batch_run` hands EVERY `child_job_ids` entry to the combined export --
including children that failed, and children force-cancelled at the deadline.
`_COMBINED_CTES` had no job-status filter, so those rows went out in the emailed
partial-batch CSV and through the in-app combined download, while Lists showed
nothing: `segments` and `analytics` both filter on done (2026-09-08). This was
the last path still open (Codex).

The per-child count in the API is the other half. It counted a failed child's
rows *because* the export had no filter -- so closing the export gap is exactly
what makes that number dishonest.

Real DB (SyncSessionLocal) -- no mocks.
"""
import uuid

from src.api.routes.batches import _child_lead_count
from src.db.models import Job, Result, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_export import _combined_pairs, compute_delivery_counts


def _user(db) -> User:
    u = User(
        id=str(uuid.uuid4()),
        email=f"bjs-{uuid.uuid4().hex[:10]}@test.local",
        password_hash="x" * 60,
        plan="pro",
        records_used=0,
        records_limit=-1,
    )
    db.add(u)
    db.flush()
    return u


def _batch(db, user_id: str) -> ScraperBatch:
    b = ScraperBatch(
        id=str(uuid.uuid4()), user_id=user_id, name="Status Test", state="WA",
        fields=[], enrichment=[], schedule={}, deliver={}, status="active",
        delivery_mode="everything",
    )
    db.add(b)
    db.flush()
    return b


def _config(db, user_id: str, batch_id: str, record_type: str) -> ScraperConfig:
    c = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, batch_id=batch_id,
        name=f"child {record_type}", county="pierce", state="WA",
        record_type=record_type, fields=[], enrichment=[], schedule={}, deliver={},
    )
    db.add(c)
    db.flush()
    return c


def _job(db, user_id: str, config_id: str, status: str) -> Job:
    j = Job(id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=config_id,
            status=status, trigger="batch")
    db.add(j)
    db.flush()
    return j


def _result(db, user_id: str, job_id: str, party: str) -> Result:
    r = Result(
        id=str(uuid.uuid4()), user_id=user_id, job_id=job_id,
        date_recorded="06/01/2026", party_name=party,
        property_address="100 MAIN ST",
        property_key=f"WA|pierce|{uuid.uuid4().hex[:10]}",
    )
    db.add(r)
    db.flush()
    return r


def _seed(db, child_status: str):
    """One done child and one child in ``child_status``, each with a lead."""
    user = _user(db)
    batch = _batch(db, user.id)
    done = _job(db, user.id, _config(db, user.id, batch.id, "probate").id, "done")
    other = _job(db, user.id, _config(db, user.id, batch.id, "tax_delinquent").id,
                 child_status)
    _result(db, user.id, done.id, "DELIVERED")
    _result(db, user.id, other.id, "NEVER DELIVERED")
    db.commit()
    return user, [done.id, other.id]


class TestCombinedExportStatusFilter:
    def test_a_failed_child_stays_out_of_the_combined_csv(self):
        with SyncSessionLocal() as db:
            user, job_ids = _seed(db, "failed")
            pairs = _combined_pairs(db, user.id, job_ids)
            assert [rec.party_name for rec, _ in pairs] == ["DELIVERED"]
            db.rollback()

    def test_a_cancelled_child_stays_out_too(self):
        """force-finalize cancels still-active children after they may have saved
        rows, so 'cancelled' is not a theoretical status here."""
        with SyncSessionLocal() as db:
            user, job_ids = _seed(db, "cancelled")
            pairs = _combined_pairs(db, user.id, job_ids)
            assert [rec.party_name for rec, _ in pairs] == ["DELIVERED"]
            db.rollback()

    def test_a_child_still_running_is_not_delivered_either(self):
        with SyncSessionLocal() as db:
            user, job_ids = _seed(db, "scraping")
            pairs = _combined_pairs(db, user.id, job_ids)
            assert [rec.party_name for rec, _ in pairs] == ["DELIVERED"]
            db.rollback()

    def test_the_emailed_counts_match_what_the_csv_contains(self):
        """compute_delivery_counts reads the same CTE, so the count the email
        quotes cannot drift from the file attached to it."""
        with SyncSessionLocal() as db:
            user, job_ids = _seed(db, "failed")
            counts = compute_delivery_counts(db, user.id, job_ids)
            assert counts["leads_total"] == 1
            db.rollback()

    def test_two_done_children_are_both_delivered(self):
        """The filter must not cost a healthy batch anything."""
        with SyncSessionLocal() as db:
            user, job_ids = _seed(db, "done")
            pairs = _combined_pairs(db, user.id, job_ids)
            assert sorted(rec.party_name for rec, _ in pairs) == [
                "DELIVERED", "NEVER DELIVERED",
            ]
            db.rollback()


class TestChildLeadCount:
    """The per-child figure the batch detail page prints."""

    def test_a_done_child_reports_its_billed_record_count(self):
        assert _child_lead_count(("job-1", "done", 42), {"job-1": 7}) == 42

    def test_a_failed_child_reports_nothing(self):
        """Its rows reach no export, no list and no per-job download — a failed
        run never writes the export_key that gates one."""
        assert _child_lead_count(("job-1", "failed", 210), {"job-1": 8}) == 0

    def test_a_cancelled_child_reports_nothing(self):
        assert _child_lead_count(("job-1", "cancelled", 5), {"job-1": 3}) == 0

    def test_an_in_flight_child_still_reports_progress(self):
        """record_count runs ahead mid-scrape and is reset to 0 on a re-queue, so
        the rows are counted instead — the run is not over and the number is not
        a delivery claim."""
        assert _child_lead_count(("job-1", "scraping", 210), {"job-1": 8}) == 8
