"""Batch overlaps-first delivery (spec 2026-07-01).

DB-backed proof of the three fixed bugs + the delivery modes:
  Bug A — a weak dedup_hash (name+date) can no longer merge two record types
          (fake overlap + silently dropped row).
  Bug B — a zero-row overlaps_only run still finalizes 'done' with counts
          (readiness no longer keyed on the R2 object).
  Modes — overlaps_only filters to pk-bucket 2+-type leads; everything keeps all.
"""
import uuid

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_export import (
    _combined_pairs,
    compute_delivery_counts,
    finalize_batch_run,
)


def _user(db) -> User:
    u = User(
        id=str(uuid.uuid4()),
        email=f"dm-{uuid.uuid4().hex[:10]}@test.local",
        password_hash="x" * 60,
        plan="pro",
        records_used=0,
        records_limit=-1,
    )
    db.add(u)
    db.flush()
    return u


def _batch(db, user_id: str, delivery_mode: str = "everything") -> ScraperBatch:
    b = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="DM Test",
        state="WA",
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
        delivery_mode=delivery_mode,
    )
    db.add(b)
    db.flush()
    return b


def _config(db, user_id: str, batch_id: str, record_type: str) -> ScraperConfig:
    c = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        batch_id=batch_id,
        name=f"child {record_type}",
        county="pierce",
        state="WA",
        record_type=record_type,
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
    )
    db.add(c)
    db.flush()
    return c


def _done_job(db, user_id: str, config_id: str) -> Job:
    j = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        scraper_config_id=config_id,
        status="done",
        trigger="batch",
    )
    db.add(j)
    db.flush()
    return j


def _result(db, user_id: str, job_id: str, *, party="JANE DOE",
            property_key=None, dedup_hash=None) -> Result:
    r = Result(
        id=str(uuid.uuid4()),
        user_id=user_id,
        job_id=job_id,
        date_recorded="06/01/2026",
        party_name=party,
        property_key=property_key,
        dedup_hash=dedup_hash,
    )
    db.add(r)
    db.flush()
    return r


def _two_type_batch(db, user, delivery_mode: str):
    """A batch with probate + tax_delinquent children, both jobs done."""
    batch = _batch(db, user.id, delivery_mode)
    c1 = _config(db, user.id, batch.id, "probate")
    c2 = _config(db, user.id, batch.id, "tax_delinquent")
    j1 = _done_job(db, user.id, c1.id)
    j2 = _done_job(db, user.id, c2.id)
    return batch, j1, j2


class TestOverlapIdentity:
    def test_weak_hash_never_bridges_record_types(self):
        """Bug A: same name+date dedup_hash in two record types = TWO rows, no overlap."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "everything")
            _result(db, user.id, j1.id, dedup_hash="weakhash1")
            _result(db, user.id, j2.id, dedup_hash="weakhash1")
            db.commit()

            pairs = _combined_pairs(db, user.id, [j1.id, j2.id])
            assert len(pairs) == 2  # was 1 (merged) before the fix
            assert all(rec.overlap_count == 1 for rec, _ in pairs)
            db.rollback()

    def test_property_key_bridges_record_types(self):
        """Same parcel in two record types = ONE row, overlap_count=2."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "everything")
            _result(db, user.id, j1.id, property_key="WA|pierce|0011223344")
            _result(db, user.id, j2.id, property_key="WA|pierce|0011223344")
            db.commit()

            pairs = _combined_pairs(db, user.id, [j1.id, j2.id])
            assert len(pairs) == 1
            rec, overlap = pairs[0]
            assert rec.overlap_count == 2
            assert overlap["lists"] == "Probate; Tax Delinquent"
            db.rollback()


class TestDeliveryModes:
    def _seed(self, db):
        """1 overlap (parcel in both types) + 1 pk singleton + 1 no-identity row."""
        user = _user(db)
        batch, j1, j2 = _two_type_batch(db, user, "overlaps_only")
        _result(db, user.id, j1.id, party="OVERLAP", property_key="WA|pierce|0000000001")
        _result(db, user.id, j2.id, party="OVERLAP", property_key="WA|pierce|0000000001")
        _result(db, user.id, j1.id, party="SINGLETON", property_key="WA|pierce|0000000002")
        _result(db, user.id, j2.id, party="NOPARCEL")
        db.commit()
        return user, batch, [j1.id, j2.id]

    def test_overlaps_only_filters_to_real_overlaps(self):
        with SyncSessionLocal() as db:
            user, _, job_ids = self._seed(db)
            pairs = _combined_pairs(db, user.id, job_ids, delivery_mode="overlaps_only")
            assert [rec.party_name for rec, _ in pairs] == ["OVERLAP"]
            db.rollback()

    def test_everything_keeps_all_overlap_first(self):
        with SyncSessionLocal() as db:
            user, _, job_ids = self._seed(db)
            pairs = _combined_pairs(db, user.id, job_ids, delivery_mode="everything")
            assert len(pairs) == 3
            assert pairs[0][0].party_name == "OVERLAP"  # overlap ranks first
            db.rollback()

    def test_counts_are_honest(self):
        with SyncSessionLocal() as db:
            user, _, job_ids = self._seed(db)
            counts = compute_delivery_counts(db, user.id, job_ids)
            assert counts == {
                "leads_total": 3,
                "overlaps_delivered": 1,
                "singletons_suppressed": 1,
                "unmatchable_no_parcel": 1,
            }
            db.rollback()


class TestEmptyStateFinalize:
    def test_zero_overlap_run_finalizes_done_with_counts(self):
        """Bug B: overlaps_only + zero overlaps => run 'done', counts stored,
        no R2 object needed (readiness comes from status, Task 5)."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "overlaps_only")
            _result(db, user.id, j1.id, dedup_hash="w1")  # no parcels anywhere
            _result(db, user.id, j2.id, dedup_hash="w2")
            run = BatchRun(
                id=str(uuid.uuid4()), batch_id=batch.id, user_id=user.id,
                status="running", child_job_ids=[j1.id, j2.id],
            )
            db.add(run)
            db.commit()
            run_id = run.id

            run = db.get(BatchRun, run_id)
            finalize_batch_run(db, run)

        with SyncSessionLocal() as db:
            run = db.get(BatchRun, run_id)
            assert run.status == "done"
            assert run.completed_at is not None
            assert run.combined_export_key is None  # nothing uploaded — nothing to store
            assert run.delivery_counts == {
                "leads_total": 2,
                "overlaps_delivered": 0,
                "singletons_suppressed": 0,
                "unmatchable_no_parcel": 2,
            }


# ─── Combined-export column completeness (Phase 2 / cross-check #1) ───────────
# The combined SQL under-selected columns, so the CSV builder blanked populated
# fields AND (missing delinquent_bill_year) shipped a fabricated 01/01/{year}
# tax date. The fix selects the full lead column set.

import csv as _csv  # noqa: E402
import io as _io  # noqa: E402
from decimal import Decimal  # noqa: E402

from src.utils.lead_export import write_lead_csv_with_overlap  # noqa: E402


def _render(pairs) -> list[dict]:
    buf = _io.StringIO()
    write_lead_csv_with_overlap(pairs, buf)
    buf.seek(0)
    return list(_csv.DictReader(buf))


class TestCombinedExportColumns:
    def test_tax_row_no_fabricated_filed_date_and_full_columns(self):
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "everything")
            _result(db, user.id, j1.id, party="PROBATE OWNER",
                    property_key="WA|pierce|000000A1")
            # tax_delinquent row: synthetic county date_recorded + real bill_year
            # + plaintext extras the old SELECT dropped.
            db.add(Result(
                id=str(uuid.uuid4()), user_id=user.id, job_id=j2.id,
                date_recorded="01/01/2024",  # synthetic — county tax has no event date
                party_name="TAX OWNER", property_key="WA|pierce|000000A2",
                delinquent_bill_year=2024, delinquent_amount=Decimal("1234.56"),
                heirs="ESTATE OF X", legal_description="LOT 1 BLK 2", doc_type="TAXLIEN",
            ))
            db.flush()
            rows = _render(_combined_pairs(db, user.id, [j1.id, j2.id], delivery_mode="everything"))
            db.rollback()

        tax = next(r for r in rows if r["party_name"] == "TAX OWNER")
        assert tax["filed_date"] == ""            # NO fabricated 01/01/2024
        assert tax["delinquent_bill_year"] == "2024"
        assert tax["delinquent_amount"] == "1234.56"
        assert tax["heirs"] == "ESTATE OF X"
        assert tax["legal_description"] == "LOT 1 BLK 2"
        assert tax["doc_type"] == "TAXLIEN"

    def test_aggregated_lead_subtype_survives_nonprobate_winner(self):
        """A pk bucket bridging probate + tax: the winning (representative) row may
        be the tax row, but the bucket's aggregated probate subtype must still be
        exported (the per-row enrichment subtype is popped so the scalar wins)."""
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, j2 = _two_type_batch(db, user, "everything")
            pk = "WA|pierce|000000B7"
            # probate row carries the subtype in enrichment_data
            db.add(Result(
                id=str(uuid.uuid4()), user_id=user.id, job_id=j1.id,
                date_recorded="06/01/2026", party_name="OWNER", property_key=pk,
                enrichment_data={"lead_subtype": "probate_death_inheritance"},
            ))
            # tax row: no subtype, newer job → likely the representative winner
            db.add(Result(
                id=str(uuid.uuid4()), user_id=user.id, job_id=j2.id,
                date_recorded="06/02/2026", party_name="OWNER", property_key=pk,
                delinquent_bill_year=2025,
            ))
            db.flush()
            rows = _render(_combined_pairs(db, user.id, [j1.id, j2.id], delivery_mode="everything"))
            db.rollback()

        assert len(rows) == 1  # one bucket (pk bridges both)
        assert rows[0]["lead_subtype"] == "probate_death_inheritance"
        assert rows[0]["filed_date"] == ""  # tax winner → bill_year present → blanked


class TestNoSilentTruncation:
    def test_combined_pairs_all_pages_past_the_cap(self, monkeypatch):
        """#2: >EXPORT_CAP deduped leads must NOT be silently truncated. With the
        cap monkeypatched to 2, three distinct-bucket leads still all come back."""
        import src.workers.batch_export as be
        monkeypatch.setattr(be, "EXPORT_CAP", 2)
        with SyncSessionLocal() as db:
            user = _user(db)
            batch, j1, _j2 = _two_type_batch(db, user, "everything")
            for i in range(3):
                _result(db, user.id, j1.id, party=f"OWNER {i}",
                        property_key=f"WA|pierce|00000C{i}")
            db.flush()
            capped = be._combined_pairs(db, user.id, [j1.id], delivery_mode="everything")
            full = be._combined_pairs_all(db, user.id, [j1.id], delivery_mode="everything")
            db.rollback()
        assert len(capped) == 2           # single query is still page-bounded
        assert len(full) == 3             # paging accumulates every lead
