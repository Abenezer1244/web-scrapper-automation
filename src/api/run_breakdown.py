"""The run-count breakdown: what happened to every record a run found.

A finished run used to show numbers that disagreed (records_found 265, the worker's
"12 new leads, 252 duplicates", the results page's 258), each counted at a different
point over a different set of rows. The breakdown names every record once:

    records_found = dropped_before_save + no_address + same_run_merged
                  + already_delivered + over_quota + new

Per SAVED row of the job, first match wins (``PARTITION_CASE``):
  no_address         no usable property or mailing address, whatever else is true
  same_run_merged    combined into another row of this same run
  already_delivered  an earlier run of the account holds the claim (results_category)
  over_quota         not a duplicate, past the plan cap
  new                not a duplicate, not over quota: EXACTLY the billing predicate
  unclassified       anything else (a 'superseded' row that has an address). Never
                     frozen: a superseded row is only ever written onto an older run
                     after it finished, so it cannot exist when a run is snapshotted.
``dropped_before_save`` = records_found - rows saved (living-TOD filter + the
save-time fingerprint merge).

The worker freezes the six in the done-CAS (migration 106) from the same statement
that produces the billed count; the API reads that snapshot, or computes the same
partition live for a finished job that has none. It is RAW: no tax cap and no view
filter, so on a tax run the "already delivered" tab (tax-capped) can be smaller.

One module for both sides: raw SQL built only from code constants, and pure
functions, so the sync worker and the async API cannot disagree. No FastAPI or
worker imports.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from src.api.lead_actionability import (
    actionable_sql,
    address_actionable_sql,
    quota_excluded_sql,
)
from src.api.results_category import already_delivered_sql

# The customer-facing order, which is also the order of the frozen columns.
BREAKDOWN_FIELDS = (
    "dropped_before_save",
    "no_address",
    "same_run_merged",
    "already_delivered",
    "over_quota",
    "new",
)
ROW_BUCKETS = BREAKDOWN_FIELDS[1:]
UNCLASSIFIED = "unclassified"
SNAPSHOT_COLUMNS = {field: f"breakdown_{field}" for field in BREAKDOWN_FIELDS}


def partition_case(alias: str) -> str:
    """ONE CASE, so a row lands in exactly one bucket by construction."""
    return (
        "CASE"
        f" WHEN NOT {address_actionable_sql(alias)} THEN 'no_address'"
        f" WHEN {alias}.is_duplicate IS TRUE AND {alias}.duplicate_reason = 'same_run'"
        "  THEN 'same_run_merged'"
        f" WHEN {already_delivered_sql(alias)} THEN 'already_delivered'"
        f" WHEN {alias}.is_duplicate = false AND {quota_excluded_sql(alias)}"
        "  THEN 'over_quota'"
        # `new` is the billing predicate verbatim, so a row the bill would not count
        # can never land here: if actionability ever gains a condition the branches
        # above do not spell, the row falls to unclassified and no snapshot is taken.
        f" WHEN {alias}.is_duplicate = false AND {actionable_sql(alias)} THEN 'new'"
        f" ELSE '{UNCLASSIFIED}' END"
    )


# Scoped by job AND user: the worker runs it as the system role, the API on the
# tenant's RLS session, and neither may count another account's rows.
PARTITION_SQL = text(
    f"SELECT {partition_case('r')} AS bucket, count(*) AS n "  # noqa: S608 -- splices only code constants; job and user are bound
    "FROM results r "
    "WHERE r.job_id = :jid AND r.user_id = CAST(:uid AS uuid) "
    "GROUP BY 1"
)


@dataclass(frozen=True)
class RowPartition:
    """Every saved row of one job, in exactly one bucket."""

    no_address: int = 0
    same_run_merged: int = 0
    already_delivered: int = 0
    over_quota: int = 0
    new: int = 0
    unclassified: int = 0

    @property
    def persisted(self) -> int:
        return sum(getattr(self, b) for b in ROW_BUCKETS) + self.unclassified


def partition_from_rows(rows) -> RowPartition:
    """A job with no saved rows returns no groups at all: every bucket is 0."""
    counts = {bucket: int(n) for bucket, n in rows}
    unknown = set(counts) - set(ROW_BUCKETS) - {UNCLASSIFIED}
    if unknown:  # the CASE above can only produce the names it spells
        raise ValueError(f"unexpected breakdown bucket(s): {sorted(unknown)}")
    return RowPartition(**counts)


def _params(job_id, user_id) -> dict[str, str]:
    return {"jid": str(job_id), "uid": str(user_id)}


def read_partition(db, job_id, user_id) -> RowPartition:
    """Sync (worker) read: one statement, one snapshot for every bucket."""
    return partition_from_rows(db.execute(PARTITION_SQL, _params(job_id, user_id)).all())


async def read_partition_async(db, job_id, user_id) -> RowPartition:
    """Async (API) read of the same statement."""
    result = await db.execute(PARTITION_SQL, _params(job_id, user_id))
    return partition_from_rows(result.all())


def _full(partition: RowPartition, dropped: int | None) -> dict[str, int | None]:
    values = {b: getattr(partition, b) for b in ROW_BUCKETS}
    return {"dropped_before_save": dropped, **values}


# ── the worker's decision: what the done-CAS writes ──────────────────────────

def snapshot_columns(
    *,
    billed_now: bool,
    attempt_started_at: datetime | None,
    row_started_at: datetime | None,
    records_found: int | None,
    retry_count: int | None,
    partition: RowPartition,
) -> tuple[dict[str, int | None] | None, str | None]:
    """The columns the done-CAS writes, and why a snapshot was refused.

    Returns ``(None, reason)`` when the done-CAS must not NAME the columns at all:
      - billed by an earlier attempt: an existing snapshot is kept, none is made;
      - this attempt no longer owns the row: it must not describe another attempt.
    Returns ``(all-None columns, reason)`` when this attempt owns a fresh bill but
    the numbers cannot be trusted, so the job reports its live breakdown.
    Returns ``(values, None)`` for a snapshot that reconciles.

    ``row_started_at``, ``records_found`` and ``retry_count`` must be read from the
    jobs row under the billing CAS's row lock, never from the ORM object.
    """
    if not billed_now:
        return None, "billed by an earlier attempt"
    if attempt_started_at is None or row_started_at != attempt_started_at:
        return None, "this attempt no longer owns the job"

    reason = None
    if records_found is None:
        reason = "records_found was not recorded"
    elif retry_count != 0:
        # records_found is per attempt, the saved rows are per job: an earlier
        # attempt's rows survive the idempotent insert, so the difference is not
        # this attempt's drop count even when it is not negative.
        reason = "the run was retried"
    elif partition.unclassified:
        reason = f"{partition.unclassified} row(s) fit no bucket"
    elif partition.persisted > records_found:
        reason = f"{partition.persisted} saved but only {records_found} found"
    if reason is not None:
        return dict.fromkeys(SNAPSHOT_COLUMNS.values()), reason

    values = _full(partition, records_found - partition.persisted)
    problem = _invalid(values, records_found)
    if problem is not None:
        return dict.fromkeys(SNAPSHOT_COLUMNS.values()), problem
    return {SNAPSHOT_COLUMNS[f]: v for f, v in values.items()}, None


_OWNER_SQL = text(
    "SELECT started_at, records_found, retry_count FROM jobs "
    "WHERE id = :jid AND user_id = CAST(:uid AS uuid)"
)


def decide_snapshot(
    db, *, job_id, user_id, billed_now: bool, attempt_started_at: datetime | None,
    partition: RowPartition,
) -> tuple[dict[str, int | None] | None, str | None]:
    """``snapshot_columns`` fed from the ROW, as run_scrape_job calls it.

    Must run inside the billing transaction, right after the billing CAS: when that
    CAS fired it holds the jobs row lock, so the attempt token, records_found and
    retry_count read here are the ones the done-CAS will commit against. When it did
    not fire, nothing is read: the columns are not named either way.
    """
    owner = (
        db.execute(_OWNER_SQL, _params(job_id, user_id)).one_or_none()
        if billed_now else None
    )
    return snapshot_columns(
        billed_now=bool(billed_now),
        attempt_started_at=attempt_started_at,
        row_started_at=owner.started_at if owner else None,
        records_found=owner.records_found if owner else None,
        retry_count=owner.retry_count if owner else None,
        partition=partition,
    )


def _invalid(values: dict[str, int | None], records_found: int) -> str | None:
    if any(v is None or v < 0 for v in values.values()):
        return "a bucket is missing or negative"
    if sum(values.values()) != records_found:
        return "the buckets do not add up to records_found"
    return None


# ── the API's reading ─────────────────────────────────────────────────────────

def breakdown_from_job(job: Any) -> tuple[dict[str, int] | None, str | None]:
    """The frozen breakdown off a jobs row, or why it cannot be shown.

    ``(None, None)``: no snapshot (all six NULL), the ordinary case for older and
    retried runs. ``(None, reason)``: a stored snapshot that does not validate, which
    cannot happen by construction; the caller logs it and shows nothing rather than
    numbers that contradict each other.
    """
    stored = {f: getattr(job, col, None) for f, col in SNAPSHOT_COLUMNS.items()}
    if all(v is None for v in stored.values()):
        return None, None
    if any(v is None for v in stored.values()):
        return None, "partial snapshot"
    records_found = getattr(job, "records_found", None)
    record_count = getattr(job, "record_count", None)
    billed_count = getattr(job, "billed_count", None)
    if records_found is None or record_count is None or billed_count is None:
        return None, "records_found, record_count or billed_count missing"
    problem = _invalid(stored, records_found)
    if problem is not None:
        return None, problem
    if not stored["new"] == record_count == billed_count:
        return None, "new does not match record_count and billed_count"
    return stored, None


def live_breakdown(
    partition: RowPartition,
    *,
    status: str,
    records_found: int | None,
    retry_count: int | None,
) -> tuple[dict[str, int | None] | None, str | None]:
    """The same partition read now, for a finished job that has no snapshot.

    Only a DONE job: before that, records_found is written ahead of the filter and
    the saves, so rows still on their way would read as "not saved".
    ``dropped_before_save`` is None (unknown) when records_found was never recorded
    or the run was retried. ``(None, reason)`` when it cannot reconcile.
    """
    if status != "done":
        return None, "the run has not finished"
    if partition.unclassified:
        return None, f"{partition.unclassified} row(s) fit no bucket"
    if records_found is None or retry_count != 0:
        return _full(partition, None), None
    if partition.persisted > records_found:
        return None, f"{partition.persisted} saved but only {records_found} found"
    return _full(partition, records_found - partition.persisted), None


# ── the worker's completion line ─────────────────────────────────────────────

_LOG_PHRASES = (
    ("already_delivered", "already delivered"),
    ("same_run_merged", "combined in this run"),
    ("no_address", "without an address"),
    ("over_quota", "over your plan limit"),
    ("dropped_before_save", "not saved"),
)


def completion_message(new: int, snapshot: dict[str, int | None] | None) -> str:
    """"Job complete: 12 new leads (246 already delivered, 7 without an address)".

    Without a snapshot it states only what was charged: a duplicate figure from any
    other count is exactly the disagreement this module exists to end.
    """
    head = f"Job complete: {new} new lead{'' if new == 1 else 's'}"
    if not snapshot:
        return head
    parts = [f"{snapshot[key]} {label}" for key, label in _LOG_PHRASES if snapshot.get(key)]
    return f"{head} ({', '.join(parts)})" if parts else head
