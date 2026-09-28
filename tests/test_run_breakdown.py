"""The run-count breakdown (src/api/run_breakdown.py): one partition of a run's rows.

Real DB (conftest `db` fixture) for everything that reads rows; the decision and
formatting functions are pure and are tested on partitions read from real rows where
the plan asks for it.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.api.lead_actionability import (
    ADDRESS_PLACEHOLDER,
    DELIVERY_EXCLUDED_KEY,
    OVER_QUOTA,
    actionable_condition,
)
from src.api.run_breakdown import (
    SNAPSHOT_COLUMNS,
    RowPartition,
    breakdown_from_job,
    completion_message,
    live_breakdown,
    read_partition,
    snapshot_columns,
)
from src.db.models import Job, Result, ScraperConfig, User

ADDR = "5006 61ST STREET CT E"
MAIL = "PO BOX 12, TACOMA WA 98401"
CAPPED = {DELIVERY_EXCLUDED_KEY: OVER_QUOTA}
STARTED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


async def _job(db, user: User, config: ScraperConfig, **kw) -> str:
    job_id = str(uuid.uuid4())
    kw.setdefault("status", "enriching")
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               trigger="manual", **kw))
    await db.commit()
    return job_id


async def _rows(db, job_id, user_id, specs) -> None:
    for spec in specs:
        spec = dict(spec)
        spec.setdefault("is_duplicate", False)
        db.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=user_id, **spec))
    await db.commit()


async def _partition(db, job_id, user_id) -> RowPartition:
    return await db.run_sync(lambda s: read_partition(s, job_id, user_id))


# Every state a saved row can be in when a run finishes, with the bucket it must
# land in. Precedence: no address beats everything, then the duplicate buckets,
# then over quota.
EVERY_STATE = [
    ({}, "no_address"),                                                    # nothing
    ({"is_duplicate": True, "duplicate_reason": "prior_run"}, "no_address"),
    ({"property_address": ADDRESS_PLACEHOLDER,
      "mailing_address": ADDRESS_PLACEHOLDER}, "no_address"),
    ({"property_address": "   "}, "no_address"),
    ({"is_duplicate": True, "duplicate_reason": "superseded"}, "no_address"),
    ({"enrichment_data": CAPPED}, "no_address"),                           # T2
    ({"property_address": ADDR, "is_duplicate": True,
      "duplicate_reason": "same_run"}, "same_run_merged"),
    ({"property_address": ADDR, "is_duplicate": True, "duplicate_reason": "same_run",
      "enrichment_data": CAPPED}, "same_run_merged"),                      # T2
    ({"property_address": ADDR, "is_duplicate": True,
      "duplicate_reason": "prior_run"}, "already_delivered"),
    ({"mailing_address": MAIL, "is_duplicate": True}, "already_delivered"),  # NULL reason
    ({"property_address": ADDR, "enrichment_data": CAPPED}, "over_quota"),
    ({"property_address": ADDR}, "new"),
    ({"mailing_address": MAIL}, "new"),
]


@pytest.mark.asyncio
async def test_every_row_state_lands_in_exactly_one_bucket(db, starter_user, scraper_config):
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [spec for spec, _ in EVERY_STATE])

    got = await _partition(db, job_id, starter_user.id)

    want = dict.fromkeys(("no_address", "same_run_merged", "already_delivered", "over_quota", "new"), 0)
    for _, bucket in EVERY_STATE:
        want[bucket] += 1
    assert got == RowPartition(**want, unclassified=0)
    assert got.persisted == len(EVERY_STATE)


@pytest.mark.asyncio
async def test_new_is_exactly_the_billing_predicate(db, starter_user, scraper_config):
    """`new` must equal what tasks.py bills: non-duplicate AND actionable."""
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [spec for spec, _ in EVERY_STATE])

    billed = (await db.execute(select(func.count()).where(
        Result.job_id == job_id, Result.user_id == starter_user.id,
        Result.is_duplicate.is_(False), actionable_condition(),
    ))).scalar_one()

    assert (await _partition(db, job_id, starter_user.id)).new == billed == 2


@pytest.mark.asyncio
async def test_superseded_row_with_an_address_is_unclassified(db, starter_user, scraper_config):
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [
        {"property_address": ADDR, "is_duplicate": True, "duplicate_reason": "superseded"},
        {"property_address": ADDR},
    ])
    got = await _partition(db, job_id, starter_user.id)
    assert (got.unclassified, got.new, got.persisted) == (1, 1, 2)


@pytest.mark.asyncio
async def test_another_accounts_rows_on_the_same_job_never_count(
    db, starter_user, business_user, scraper_config,
):
    job_id = await _job(db, starter_user, scraper_config)
    await _rows(db, job_id, starter_user.id, [{"property_address": ADDR}])
    await _rows(db, job_id, business_user.id, [{"property_address": ADDR}] * 3)

    assert (await _partition(db, job_id, starter_user.id)).persisted == 1


@pytest.mark.asyncio
async def test_empty_run_is_six_zeros_that_validate(db, starter_user, scraper_config):
    job_id = await _job(db, starter_user, scraper_config)
    partition = await _partition(db, job_id, starter_user.id)
    assert partition == RowPartition()

    cols, reason = snapshot_columns(
        billed_now=True, attempt_started_at=STARTED, row_started_at=STARTED,
        records_found=0, retry_count=0, partition=partition,
    )
    assert reason is None
    assert cols == dict.fromkeys(SNAPSHOT_COLUMNS.values(), 0)

    job = Job(records_found=0, record_count=0, billed_count=0, **cols)
    assert breakdown_from_job(job) == (dict.fromkeys(SNAPSHOT_COLUMNS, 0), None)


# ── the done-CAS decision: one test per branch ───────────────────────────────

def _decide(base, **over):
    kw = {"billed_now": True, "attempt_started_at": STARTED, "row_started_at": STARTED,
          "records_found": base.persisted + 2, "retry_count": 0, "partition": base}
    kw.update(over)
    return snapshot_columns(**kw)


P = RowPartition(no_address=7, already_delivered=246, new=12)


def test_owner_with_a_reconciling_partition_writes_the_six_values():
    cols, reason = _decide(P, records_found=267)
    assert reason is None
    assert cols == {
        "breakdown_dropped_before_save": 2, "breakdown_no_address": 7,
        "breakdown_same_run_merged": 0, "breakdown_already_delivered": 246,
        "breakdown_over_quota": 0, "breakdown_new": 12,
    }
    assert sum(cols.values()) == 267


def test_billed_by_an_earlier_attempt_names_no_column():
    cols, reason = _decide(P, billed_now=False)
    assert cols is None and reason


def test_lost_ownership_names_no_column():
    cols, reason = _decide(P, row_started_at=STARTED + timedelta(minutes=9))
    assert cols is None and reason
    cols, reason = _decide(P, row_started_at=None)
    assert cols is None and reason


@pytest.mark.parametrize("over", [
    {"records_found": None},
    {"retry_count": 1},
    {"retry_count": None},
    {"records_found": P.persisted - 1},
    {"partition": RowPartition(new=1, unclassified=1)},
])
def test_owner_with_untrustworthy_numbers_writes_six_nulls(over):
    cols, reason = _decide(P, **over)
    assert cols == dict.fromkeys(SNAPSHOT_COLUMNS.values())
    assert reason


@pytest.mark.asyncio
async def test_retried_run_is_refused_even_when_the_difference_looks_valid(
    db, starter_user, scraper_config,
):
    """An earlier attempt's rows survive the idempotent insert, so after a retry
    records_found - persisted is not this attempt's drop count even when >= 0."""
    job_id = await _job(db, starter_user, scraper_config, retry_count=1)
    await _rows(db, job_id, starter_user.id, [{"property_address": ADDR}] * 3)
    partition = await _partition(db, job_id, starter_user.id)

    cols, reason = _decide(partition, records_found=5, retry_count=1)
    assert cols == dict.fromkeys(SNAPSHOT_COLUMNS.values())
    assert "retried" in reason


# ── reading a stored snapshot ────────────────────────────────────────────────

def _stored(**over):
    base = {"records_found": 267, "record_count": 12, "billed_count": 12,
            "breakdown_dropped_before_save": 2, "breakdown_no_address": 7,
            "breakdown_same_run_merged": 0, "breakdown_already_delivered": 246,
            "breakdown_over_quota": 0, "breakdown_new": 12}
    base.update(over)
    return Job(**base)


def test_a_valid_snapshot_is_returned():
    got, reason = breakdown_from_job(_stored())
    assert reason is None
    assert got == {"dropped_before_save": 2, "no_address": 7, "same_run_merged": 0,
                   "already_delivered": 246, "over_quota": 0, "new": 12}


def test_no_snapshot_is_not_an_error():
    assert breakdown_from_job(Job(records_found=5, record_count=1, billed_count=1)) == (None, None)


@pytest.mark.parametrize("over", [
    {"breakdown_over_quota": None},                         # partial
    {"breakdown_no_address": -1, "breakdown_dropped_before_save": 10},
    {"breakdown_no_address": 8},                            # does not add up
    {"records_found": None},
    {"record_count": None},
    {"billed_count": None},
    {"record_count": 11},
    {"billed_count": 13},
])
def test_an_invalid_snapshot_is_rejected_with_a_reason(over):
    got, reason = breakdown_from_job(_stored(**over))
    assert got is None and reason


# ── the live reading ─────────────────────────────────────────────────────────

def test_live_only_for_a_finished_run():
    assert live_breakdown(P, status="enriching", records_found=267, retry_count=0)[0] is None


def test_live_reconciles_for_a_clean_done_run():
    got, reason = live_breakdown(P, status="done", records_found=267, retry_count=0)
    assert reason is None and got["dropped_before_save"] == 2 and got["new"] == 12


@pytest.mark.parametrize("records_found, retry_count", [(None, 0), (267, 1)])
def test_live_drop_count_is_unknown_without_records_found_or_after_a_retry(
    records_found, retry_count,
):
    got, reason = live_breakdown(P, status="done", records_found=records_found,
                                 retry_count=retry_count)
    assert reason is None
    assert got["dropped_before_save"] is None
    assert (got["no_address"], got["already_delivered"], got["new"]) == (7, 246, 12)


def test_live_is_unavailable_when_it_cannot_reconcile():
    assert live_breakdown(P, status="done", records_found=P.persisted - 1, retry_count=0)[0] is None
    assert live_breakdown(RowPartition(new=1, unclassified=1), status="done",
                          records_found=5, retry_count=0)[0] is None


# ── the worker's completion line ─────────────────────────────────────────────

def test_completion_message_without_a_snapshot_states_only_the_charge():
    assert completion_message(12, None) == "Job complete: 12 new leads"


def test_completion_message_all_zero_has_no_parentheses_and_singular_lead():
    zero = dict.fromkeys(SNAPSHOT_COLUMNS, 0)
    assert completion_message(1, {**zero, "new": 1}) == "Job complete: 1 new lead"
    assert completion_message(0, zero) == "Job complete: 0 new leads"


def test_completion_message_lists_only_non_zero_buckets_in_fixed_order():
    one = dict.fromkeys(SNAPSHOT_COLUMNS, 0) | {"new": 12, "no_address": 7}
    assert completion_message(12, one) == "Job complete: 12 new leads (7 without an address)"

    every = {"dropped_before_save": 1, "no_address": 7, "same_run_merged": 3,
             "already_delivered": 246, "over_quota": 4, "new": 12}
    assert completion_message(12, every) == (
        "Job complete: 12 new leads (246 already delivered, 3 combined in this run, "
        "7 without an address, 4 over your plan limit, 1 not saved)"
    )
