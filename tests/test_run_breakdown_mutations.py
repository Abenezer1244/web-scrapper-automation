"""Mutation checks for the run-count partition and the snapshot's ownership guard,
run on every suite (not by hand).

Each test builds the production statement with ONE guard removed and runs it on the
same real rows the partition tests use. If the mutant returned the same answer, the
fixtures could not tell the guard is there and the partition tests would stay green
without it. So every mutant must disagree with the real statement.

`new`'s `AND actionable_sql(...)` is deliberately not mutated: with the branches above
it, it is equivalent today by construction (it only matters if actionability ever
gains a condition those branches do not spell, which is exactly what it guards).
"""
import inspect
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.api import run_breakdown
from src.api.run_breakdown import (
    PARTITION_SQL,
    RowPartition,
    partition_case,
    partition_from_rows,
)
from src.workers.tasks_helpers.status import AttemptToken
from tests.test_run_breakdown import ADDR, EVERY_STATE, _job, _partition, _rows

BRANCH_MARKERS = {
    "no_address": "THEN 'no_address'",
    "same_run_merged": "THEN 'same_run_merged'",
    "already_delivered": "THEN 'already_delivered'",
    "over_quota": "THEN 'over_quota'",
}


def _without_branch(case_sql: str, marker: str) -> str:
    """The CASE with the one WHEN ... THEN <bucket> clause removed."""
    head, tail = case_sql.split(marker, 1)
    start = head.rindex(" WHEN ")
    return head[:start] + tail


async def _run(db, sql: str, job_id, user_id):
    rows = (await db.execute(text(sql), {"jid": job_id, "uid": str(user_id)})).all()
    return partition_from_rows(rows)


def _statement(case_sql: str, *, scoped_to_user: bool = True) -> str:
    user = " AND r.user_id = CAST(:uid AS uuid)" if scoped_to_user else ""
    return (f"SELECT {case_sql} AS bucket, count(*) AS n FROM results r "  # noqa: S608 -- test-only; splices only the production CASE
            f"WHERE r.job_id = :jid{user} GROUP BY 1")


@pytest.mark.asyncio
async def test_the_harness_reproduces_the_production_statement(
    db, starter_user, scraper_config,
):
    """Guards the harness itself: unmutated, it must equal PARTITION_SQL's answer."""
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [spec for spec, _ in EVERY_STATE])
    assert " ".join(PARTITION_SQL.text.split()) == " ".join(_statement(partition_case("r")).split())
    assert await _run(db, _statement(partition_case("r")), job_id, starter_user.id) == \
        await _partition(db, job_id, starter_user.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket", sorted(BRANCH_MARKERS))
async def test_removing_any_case_branch_changes_the_partition(
    db, starter_user, scraper_config, bucket,
):
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [spec for spec, _ in EVERY_STATE])

    real = await _partition(db, job_id, starter_user.id)
    mutant = await _run(db, _statement(_without_branch(partition_case("r"),
                                                       BRANCH_MARKERS[bucket])),
                        job_id, starter_user.id)
    assert mutant != real, f"the fixtures cannot tell the {bucket} branch is there"


@pytest.mark.asyncio
async def test_removing_the_user_predicate_changes_the_partition(
    db, starter_user, business_user, scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [{"property_address": ADDR}])
    await _rows(db, job_id, business_user.id, [{"property_address": ADDR}])

    real = await _partition(db, job_id, starter_user.id)
    mutant = await _run(db, _statement(partition_case("r"), scoped_to_user=False),
                        job_id, starter_user.id)
    assert (real.persisted, mutant.persisted) == (1, 2)


# ── the ownership guard of the done-CAS decision (Codex 2c diff r3) ──────────
# A Python guard, so each mutant is snapshot_columns rebuilt from its own source
# with ONE ownership condition replaced by False, run on the case it exists for.

OWNERSHIP_GUARDS = {
    "started_at": "row_started_at != token_started",
    "retry_count": "(token_retry is not None and retry_count != token_retry)",
}
_STARTED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_P = RowPartition(new=3)


def _mutant(guard: str):
    source = inspect.getsource(run_breakdown.snapshot_columns)
    assert source.count(OWNERSHIP_GUARDS[guard]) == 1, "the guard moved; update the harness"
    namespace = dict(vars(run_breakdown))
    exec(source.replace(OWNERSHIP_GUARDS[guard], "False"), namespace)  # noqa: S102 -- test-only; the production function's own source
    return namespace["snapshot_columns"]


@pytest.mark.parametrize(("guard", "row"), [
    # the row was re-claimed at a different instant
    ("started_at", {"row_started_at": _STARTED + timedelta(minutes=9), "retry_count": 0}),
    # the row was re-claimed at A's exact instant, one claim later
    ("retry_count", {"row_started_at": _STARTED, "retry_count": 1}),
])
def test_removing_an_ownership_condition_lets_a_stale_attempt_snapshot(guard, row):
    kw = {"billed_now": True, "attempt_token": AttemptToken(_STARTED, 0),
          "records_found": 3, "partition": _P, **row}

    real, reason = run_breakdown.snapshot_columns(**kw)
    assert real is None and "no longer owns" in reason
    mutant, _ = _mutant(guard)(**kw)
    assert mutant is not None, f"the fixtures cannot tell the {guard} condition is there"
