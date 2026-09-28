"""Mutation checks for the run-count partition, run on every suite (not by hand).

Each test builds the production statement with ONE guard removed and runs it on the
same real rows the partition tests use. If the mutant returned the same answer, the
fixtures could not tell the guard is there and the partition tests would stay green
without it. So every mutant must disagree with the real statement.

`new`'s `AND actionable_sql(...)` is deliberately not mutated: with the branches above
it, it is equivalent today by construction (it only matters if actionability ever
gains a condition those branches do not spell, which is exactly what it guards).
"""
import pytest
from sqlalchemy import text

from src.api.run_breakdown import PARTITION_SQL, partition_case, partition_from_rows
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
