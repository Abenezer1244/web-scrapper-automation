"""Piece 2 Phase 2A.1 — batch model shape (pure, no DB).

DB-applied verification (migration 050) runs in CI like Piece 1.
"""
from src.db.models import BatchRun, ScraperBatch, ScraperConfig


def test_table_names():
    assert ScraperBatch.__tablename__ == "scraper_batches"
    assert BatchRun.__tablename__ == "batch_runs"


def test_scraper_config_has_nullable_batch_id():
    col = ScraperConfig.__table__.c["batch_id"]
    assert col.nullable is True


def test_batch_holds_shared_config():
    cols = ScraperBatch.__table__.c
    for c in ("fields", "enrichment", "schedule", "deliver", "state", "user_id"):
        assert c in cols


def test_batch_run_state_machine_fields():
    cols = BatchRun.__table__.c
    assert cols["status"].default.arg == "pending"
    for c in ("child_job_ids", "combined_export_key", "excluded_no_date_count",
              "failed_children", "completed_at", "batch_id", "user_id"):
        assert c in cols


def test_batch_run_durability_columns():
    """Track A: durable-state columns for crash recovery (migration 051)."""
    cols = BatchRun.__table__.c
    for c in ("claim_token", "dispatch_attempts", "delivery_started_at", "claimed_at", "running_at"):
        assert c in cols
    assert cols["dispatch_attempts"].nullable is False
    assert cols["delivery_started_at"].nullable is True
    assert cols["claim_token"].nullable is True


class TestDeliveryModeColumns:
    def test_scraper_batch_delivery_mode_default(self):
        from src.db.models import ScraperBatch

        col = ScraperBatch.__table__.c.delivery_mode
        assert col.nullable is False
        assert col.server_default is not None
        # Python-side default protects non-API writers (tests/scheduler).
        assert col.default.arg == "everything"

    def test_scraper_batch_delivery_mode_check_constraint(self):
        from sqlalchemy import CheckConstraint

        from src.db.models import ScraperBatch

        checks = [
            c for c in ScraperBatch.__table__.constraints
            if isinstance(c, CheckConstraint)
            and c.name == "ck_scraper_batches_delivery_mode"
        ]
        assert len(checks) == 1

    def test_batch_run_delivery_counts_column(self):
        from src.db.models import BatchRun

        col = BatchRun.__table__.c.delivery_counts
        assert col.nullable is True
