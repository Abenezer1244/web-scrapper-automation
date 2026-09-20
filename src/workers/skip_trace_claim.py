"""The single way a lead is claimed into the skip-trace queue.

Two callers will use this: the scrape enqueue (`_enqueue_skip_trace_rows`) and,
from Phase 1b-2, the "look up contacts" action worker. They MUST share it, or
the two paths drift and the same lead is claimed twice.

Why this exists at all (Codex round 15, finding 15-1). Migration 099 adds a
PARTIAL UNIQUE INDEX on ``pending_skip_trace_rows (result_id)`` WHERE status IN
('queued','submitting','submitted'). Before this module, the scrape enqueue
built rows with ``db.add()`` in a loop and flushed them all at ONE ``db.commit()``
whose handler was::

    try: db.commit()
    except Exception: db.rollback(); db.commit()

With the index in place, one conflicting row anywhere in a job's batch raises
IntegrityError at that commit, the handler rolls back THE WHOLE JOB'S enqueue -
every pending row and every ``results.skip_trace_status = 'queued'`` update -
and then commits an empty transaction. Silent, total, unreported loss of a
job's lookups. So the claim is expressed as ONE set-based statement that tells
us exactly which rows it won:

    INSERT ... VALUES (...), (...)
    ON CONFLICT (result_id) WHERE status IN (...) DO NOTHING
    RETURNING result_id

and ``results`` is advanced to 'queued' for EXACTLY the returned ids. A lost
race claims nothing and strands nothing, because the losing row was never
inserted and its Result was never moved off 'not_attempted'.

LOCK ORDER (15-12). Every writer that touches both tables takes them in ONE
order: ``results`` rows first, ascending by ``(id, user_id)``, then
``pending_skip_trace_rows``. ``ORDER BY id`` inside a single UPDATE is not a
global lock order and ``INSERT ... ON CONFLICT`` may take unique-index locks in
a different order than the values were supplied, so the ordering is made
explicit with a ``SELECT ... ORDER BY ... FOR UPDATE`` before the insert. Any
future writer of this pair must follow the same order.

THE CALLER OWNS THE TRANSACTION. Nothing here commits. The action worker needs
the claim, the action-result dispositions and the audit event to land in one
transaction, which is impossible if the helper commits underneath it.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

# The pending statuses that mean "this lead is already being looked up". Must
# stay identical to the predicate of the partial unique index in migration 099
# and to the dispatcher's notion of an active row; a mismatch either lets a
# second claim through (double charge) or blocks a legitimate one forever.
ACTIVE_PENDING_STATUSES: tuple[str, ...] = ("queued", "submitting", "submitted")

_ACTIVE_SQL = "('queued','submitting','submitted')"

# The columns build_pending_row_payload produces, in insert order. Listed
# explicitly rather than derived from the payload dict so a stray key can never
# widen the INSERT, and so the truncation below cannot silently miss a column.
_COLUMNS: tuple[str, ...] = (
    "job_id", "result_id", "user_id",
    "property_address", "city", "state", "zip",
    "first_name", "last_name",
    "mail_address", "mail_city", "mail_state", "mail_zip",
    "trace_type",
)

# Column widths from src/db/models.py::PendingSkipTraceRow. Truncating HERE, in
# the one place that writes the row, is what keeps the Phase 1a subject key
# honest: lookup_subject_key truncates to these same widths before hashing, so
# the enqueue's cache read and this write hash identical bytes. A code-violation
# description of 250+ chars would otherwise raise StringDataRightTruncation,
# poison the session with PendingRollbackError and hang the job.
_WIDTHS: dict[str, int] = {
    "property_address": 512, "mail_address": 512,
    "city": 128, "state": 2, "zip": 16,
    "first_name": 128, "last_name": 128,
    "mail_city": 128, "mail_state": 2, "mail_zip": 16,
    "trace_type": 16,
}


def _truncate(column: str, value: Any) -> Any:
    width = _WIDTHS.get(column)
    if width is None or value is None:
        return value
    text_value = str(value)
    return text_value[:width] if len(text_value) > width else text_value


def claim_skip_trace_rows(
    db,
    payloads: list[dict],
    *,
    action_id: str | None = None,
) -> list[str]:
    """Claim `payloads` into the queue. Returns the result ids actually won.

    `payloads` are ``build_pending_row_payload`` dicts. Each one's `result_id`
    must already have been checked eligible by the caller; this function is the
    ATOMIC half of that decision, not the whole of it -- it guarantees only that
    no second active pending row is created for a lead that already has one.

    `action_id` stamps the rows a "look up contacts" action claimed, so a
    concurrent scrape is never counted as that action's work. The scrape path
    leaves it NULL.

    Does NOT commit. The caller owns the transaction.
    """
    if not payloads:
        return []

    # Deduplicate WITHIN the batch first. ON CONFLICT DO NOTHING cannot resolve
    # two conflicting rows inside ONE statement -- Postgres raises
    # "ON CONFLICT DO UPDATE command cannot affect row a second time" for
    # DO UPDATE, and for DO NOTHING the second row is simply dropped, but only
    # when the conflict is against an already-committed row. Two rows for the
    # same result_id in the same VALUES list are both inserted when no index
    # entry exists yet. The same job listing one lead twice is not hypothetical:
    # the trustee_sale collapse produces sibling rows for one property.
    by_result: dict[str, dict] = {}
    for payload in payloads:
        by_result.setdefault(str(payload["result_id"]), payload)
    ordered = [by_result[k] for k in sorted(by_result)]

    user_ids = {str(p["user_id"]) for p in ordered}
    if len(user_ids) != 1:
        # One claim is one tenant's work. A mixed batch would make the single
        # tenant-scoped UPDATE below either too broad or silently partial.
        raise ValueError(
            f"claim_skip_trace_rows: expected one user_id, got {len(user_ids)}"
        )
    user_id = next(iter(user_ids))
    result_ids = [str(p["result_id"]) for p in ordered]

    # LOCK ORDER step 1: results first, ascending, before any pending write.
    # Rows already gone (deleted job) simply do not come back and their insert
    # is refused by the foreign key, which is the correct outcome.
    db.execute(
        text(
            "SELECT id FROM results "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND user_id = CAST(:uid AS uuid) "
            "ORDER BY id FOR UPDATE"
        ),
        {"ids": result_ids, "uid": user_id},
    )

    # LOCK ORDER step 2: the pending rows. One statement, so a conflict is a
    # row-level no-op rather than a batch-level failure.
    #
    # `id` is supplied explicitly. PendingSkipTraceRow.id carries a PYTHON-side
    # default (`default=_uuid` in src/db/models.py), not a server default, so a
    # raw INSERT that bypasses the ORM would write NULL and violate the primary
    # key. Generated here rather than with gen_random_uuid() so the value is the
    # same shape the ORM writes and needs no database extension.
    columns = ["id", *_COLUMNS, "status"]
    if action_id is not None:
        columns.append("action_id")
    placeholders = []
    params: dict[str, Any] = {}
    for i, payload in enumerate(ordered):
        params[f"id_{i}"] = str(uuid4())
        row_slots = [f"CAST(:id_{i} AS uuid)"]
        for column in _COLUMNS:
            key = f"{column}_{i}"
            params[key] = _truncate(column, payload.get(column))
            cast = {
                "job_id": "uuid", "result_id": "uuid", "user_id": "uuid",
            }.get(column)
            row_slots.append(f"CAST(:{key} AS {cast})" if cast else f":{key}")
        row_slots.append("'queued'")
        if action_id is not None:
            row_slots.append("CAST(:action_id AS uuid)")
        placeholders.append(f"({', '.join(row_slots)})")
    if action_id is not None:
        params["action_id"] = action_id

    # Inserted ALREADY 'queued' -- an ACTIVE status, so the partial unique index
    # applies at insert time. Inserting in an inactive status first and
    # activating later would sit outside the index and let two rows collide
    # afterwards, when there is no longer a statement that can refuse one.
    claimed = db.execute(
        text(
            # noqa: S608 - nothing interpolated here is input. `columns` is
            # _COLUMNS plus the literals 'status'/'action_id'; `placeholders`
            # holds only generated ":name" bind slots and CAST types built from
            # those same literals; _ACTIVE_SQL is a module constant. EVERY
            # payload value travels in `params` as a bound parameter. The repo
            # uses the same shape in results_category.already_delivered_sql.
            f"INSERT INTO pending_skip_trace_rows ({', '.join(columns)}) "  # noqa: S608
            f"VALUES {', '.join(placeholders)} "
            f"ON CONFLICT (result_id) WHERE status IN {_ACTIVE_SQL} DO NOTHING "
            f"RETURNING result_id"
        ),
        params,
    ).scalars().all()
    claimed_ids = [str(r) for r in claimed]
    if not claimed_ids:
        return []

    # Advance ONLY the rows the insert won, and only from 'not_attempted': a
    # lead that settled between the eligibility read and here keeps its answer.
    db.execute(
        text(
            "UPDATE results SET skip_trace_status = 'queued' "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND user_id = CAST(:uid AS uuid) "
            "  AND skip_trace_status = 'not_attempted'"
        ),
        {"ids": sorted(claimed_ids), "uid": user_id},
    )
    return claimed_ids
