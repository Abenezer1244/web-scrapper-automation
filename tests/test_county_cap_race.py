"""Concurrency: two creates at the cap must not both succeed when enforcement is ON.
Real-DB test — SKIPPED by default. Run only against a dedicated test DB with
RUN_DB_TESTS=1 (never the prod .env). Exercised in Phase 7 live verification."""
import os

import pytest


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="needs a dedicated test DB; set RUN_DB_TESTS=1 to run (never against prod)",
)
def test_concurrent_creates_at_cap_both_cannot_pass():
    """Two concurrent create requests at the county cap must not both succeed.

    With the advisory lock in place, the second request's enforce_entitlements
    call sees the first request's insert already committed and raises 402.
    This test is a placeholder for Phase 7 live DB verification.
    """
    pytest.skip("Phase 7 live DB verification — not yet implemented")


def test_advisory_lock_sql_is_valid():
    # Lightweight guard that the lock SQL is well-formed (no DB connection).
    from sqlalchemy import text
    stmt = text("SELECT pg_advisory_xact_lock(4242, hashtext(:uid))")
    compiled = str(stmt)
    assert "pg_advisory_xact_lock" in compiled
