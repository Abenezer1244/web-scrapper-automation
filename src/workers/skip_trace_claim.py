"""The single way a lead is claimed into the skip-trace queue.

Two callers use this: the scrape enqueue (`_enqueue_skip_trace_rows`) and, from
Phase 1b-2, the "look up contacts" action worker. They MUST share it, or the two
paths drift and the same lead is claimed -- and charged for -- twice.

Why this exists (Codex round 15, finding 15-1). Migration 099 adds a PARTIAL
UNIQUE INDEX on ``pending_skip_trace_rows (result_id)`` WHERE status IN
('queued','submitting','submitted'). Before this module the scrape enqueue built
rows with ``db.add()`` in a loop and flushed them all at ONE ``db.commit()``
whose handler was ``except Exception: db.rollback(); db.commit()``. With the
index in place, one conflicting row anywhere in a job's batch would roll back THE
WHOLE JOB'S enqueue and then commit an empty transaction: silent, total,
unreported loss. So the claim is ONE set-based statement that reports exactly
which leads it won, and `results` is advanced for those and only those.

THE CLAIM IS ITS OWN ELIGIBILITY CHECK (round 15 diff review). The insert does
not trust its payloads. It selects through a join to `results`, so a lead only
gets a pending row when that row still EXISTS, still belongs to the claiming
tenant, and is still 'not_attempted'. Without that join a payload naming a
deleted result kills the whole batch with a foreign-key error; a payload naming
another tenant's result inserts a pending row its real owner could then never
claim (the unique index blocks them) while the dispatcher's tenant-pinned joins
ignore it forever; and a lead that settled to 'hit' between the eligibility read
and the claim gets an active queued row that nothing will ever settle.

NO ARBITER ON THE CONFLICT CLAUSE, DELIBERATELY. ``ON CONFLICT (result_id)
WHERE ...`` requires the partial index to exist and raises "no unique or
exclusion constraint matching the ON CONFLICT specification" when it does not.
`start.sh` deliberately starts the WORKER even when migrations fail (it fails
open, on the documented grounds that doing so "leaves the worker exactly where it
is today"), and `tasks.py` catches an enqueue failure and merely logs it. A
targeted conflict clause would therefore turn a failed migration 099 into every
skip-trace enqueue silently ceasing -- the one outcome that reasoning assumed
could not happen. Bare ``ON CONFLICT DO NOTHING`` degrades to exactly today's
behaviour when the index is absent and enforces when it is present.
`warn_if_unenforced` is how that degradation gets said out loud.

LOCK ORDER. Pending rows first, then `results` -- the same order
``_cancel_undeliverable_queued`` uses (it UPDATEs pending rows, then UPDATEs
`results`). An earlier version of this helper took ``SELECT ... FOR UPDATE`` on
`results` FIRST, which bought nothing (the index plus ON CONFLICT already
arbitrates uniqueness) and inverted the order against the sweep, so a sweep
holding pending rows and waiting on results could deadlock against a claim
holding results and waiting on the index. Any future writer of this pair must
take pending before results.

THE CALLER OWNS THE TRANSACTION. Nothing here commits. The action worker needs
the claim, the action-result dispositions and the audit event in ONE transaction,
which is impossible if the helper commits underneath it.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import text

from src.utils.logger import setup_logger

_logger = setup_logger(__name__)

# The pending statuses that mean "this lead is already being looked up". Must
# stay identical to the predicate of the partial unique index in migration 099;
# a mismatch either lets a second claim through (double charge) or blocks a
# legitimate one forever. test_the_active_predicate_matches_the_index pins it.
ACTIVE_PENDING_STATUSES: tuple[str, ...] = ("queued", "submitting", "submitted")

INDEX_NAME = "uq_pending_skip_trace_active_result"

# Only a lead that has never been looked up may be claimed. The scrape enqueue
# already filters on this; repeating it INSIDE the insert is what makes the
# decision atomic rather than advisory.
CLAIMABLE_RESULT_STATUS = "not_attempted"

# The columns build_pending_row_payload produces, in insert order. Listed
# explicitly rather than derived from the payload dict so a stray key can never
# widen the INSERT.
_COLUMNS: tuple[str, ...] = (
    "job_id", "result_id", "user_id",
    "property_address", "city", "state", "zip",
    "first_name", "last_name",
    "mail_address", "mail_city", "mail_state", "mail_zip",
    "trace_type",
)

_UUID_COLUMNS = frozenset({"job_id", "result_id", "user_id"})

# Column widths from src/db/models.py::PendingSkipTraceRow, for the fields the
# Phase 1a subject key hashes at the SAME width (_SUBJECT_ADDRESS_MAX = 512,
# _SUBJECT_FIELD_MAX = 128). Truncating here, in the one place that writes the
# row, is what keeps read and write hashing identical bytes: a 128-truncated
# write key would never match the untruncated read key and every repeat trace
# would be re-paid. A code-violation description of 250+ chars would otherwise
# raise StringDataRightTruncation and poison the session.
#
# `state` and `mail_state` are deliberately ABSENT. Their column is String(2)
# but lookup_subject_key hashes state at _SUBJECT_FIELD_MAX = 128, so silently
# cutting a longer value down to two characters would STORE one value and HASH
# another -- the exact divergence this table exists to prevent. A state that does
# not fit is a bug upstream in build_pending_row_payload, so the payload is
# REFUSED and reported rather than quietly mangled.
_WIDTHS: dict[str, int] = {
    "property_address": 512, "mail_address": 512,
    "city": 128, "zip": 16,
    "first_name": 128, "last_name": 128,
    "mail_city": 128, "mail_zip": 16,
    "trace_type": 16,
}

_EXACT: dict[str, int] = {"state": 2, "mail_state": 2}


def warn_if_unenforced(db) -> bool:
    """True when migration 099's unique index is present AND valid.

    The claim degrades to pre-099 behaviour without it rather than failing every
    enqueue (see the module docstring), so this is how that degradation gets said
    out loud instead of being discovered by a double charge.
    """
    enforced = bool(db.execute(
        text(
            "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = :n AND i.indisunique AND i.indisvalid"
        ),
        {"n": INDEX_NAME},
    ).scalar())
    if not enforced:
        _logger.error(
            "Skip-trace claim is running UNENFORCED: %s is missing or invalid, so "
            "nothing stops a second active claim for one lead and a lead can be "
            "charged for twice. Apply migration 099.",
            INDEX_NAME,
        )
    return enforced


def _truncate(column: str, value: Any) -> Any:
    width = _WIDTHS.get(column)
    if width is None or value is None:
        return value
    text_value = str(value)
    return text_value[:width] if len(text_value) > width else text_value


def _fits_exact(payload: dict) -> bool:
    for column, width in _EXACT.items():
        value = payload.get(column)
        if value is not None and len(str(value)) > width:
            return False
    return True


def claim_skip_trace_rows(
    db,
    payloads: list[dict],
    *,
    action_id: str | None = None,
) -> list[str]:
    """Claim `payloads` into the queue. Returns the result ids actually won.

    `payloads` are ``build_pending_row_payload`` dicts. A payload whose lead no
    longer exists, belongs to another tenant, or is no longer 'not_attempted' is
    simply not claimed -- the insert filters it, so it can neither fail the batch
    nor strand an active row nobody will settle.

    `action_id` stamps the rows a "look up contacts" action claimed, so a
    concurrent scrape is never counted as that action's work. The scrape path
    leaves it NULL.

    Does NOT commit. The caller owns the transaction.
    """
    if not payloads:
        return []

    # One claim is one tenant's work: the single tenant-scoped UPDATE that
    # advances `results` would otherwise be too broad or silently partial.
    user_ids = {str(p["user_id"]) for p in payloads}
    if len(user_ids) != 1:
        raise ValueError(
            f"claim_skip_trace_rows: expected one user_id, got {len(user_ids)}"
        )
    user_id = next(iter(user_ids))

    # Refuse rather than mangle a value whose column cannot hold it (see _EXACT).
    usable, refused = [], []
    for payload in payloads:
        (usable if _fits_exact(payload) else refused).append(payload)
    if refused:
        # Result ids only: never the homeowner's name or address in a log line.
        _logger.warning(
            "Skip-trace claim refused %d payload(s) whose state field exceeds its "
            "column; result ids: %s",
            len(refused), [str(p.get("result_id")) for p in refused][:20],
        )
    if not usable:
        return []

    # Deduplicate within the batch so the statement is deterministic about WHICH
    # payload wins for a repeated lead. ON CONFLICT DO NOTHING would also drop
    # the loser, but then which row survives depends on evaluation order. A job
    # listing one lead twice is not hypothetical: the trustee_sale collapse
    # produces sibling rows for one property.
    by_result: dict[str, dict] = {}
    for payload in usable:
        by_result.setdefault(str(payload["result_id"]), payload)
    ordered = [by_result[k] for k in sorted(by_result)]

    columns = ["id", *_COLUMNS]
    rows_sql: list[str] = []
    params: dict[str, Any] = {"uid": user_id, "claimable": CLAIMABLE_RESULT_STATUS}
    for i, payload in enumerate(ordered):
        # `id` is supplied explicitly: PendingSkipTraceRow.id carries a
        # PYTHON-side default (`default=_uuid`), not a server default, so a raw
        # INSERT that bypasses the ORM would write NULL and violate the key.
        params[f"id_{i}"] = str(uuid4())
        slots = [f"CAST(:id_{i} AS uuid)"]
        for column in _COLUMNS:
            key = f"{column}_{i}"
            params[key] = _truncate(column, payload.get(column))
            cast = "uuid" if column in _UUID_COLUMNS else "text"
            slots.append(f"CAST(:{key} AS {cast})")
        rows_sql.append(f"({', '.join(slots)})")

    select_list = ", ".join(f"v.{c}" for c in columns)
    extra_cols, extra_vals = "", ""
    if action_id is not None:
        params["action_id"] = action_id
        extra_cols, extra_vals = ", action_id", ", CAST(:action_id AS uuid)"

    # Inserted ALREADY 'queued' -- an ACTIVE status, so the partial unique index
    # applies at insert time. Inserting inactive first and activating later would
    # sit outside the index and let two rows collide afterwards, when no
    # statement is left that could refuse one.
    #
    # noqa: S608 - nothing interpolated is input. `columns`/`select_list` come
    # from the _COLUMNS literals, `rows_sql` holds only generated ":name" bind
    # slots and their CAST types, and `extra_cols`/`extra_vals` are literals.
    # EVERY payload value travels in `params` as a bound parameter.
    claimed = db.execute(
        text(
            f"INSERT INTO pending_skip_trace_rows ({', '.join(columns)}, status{extra_cols}) "  # noqa: S608
            f"SELECT {select_list}, 'queued'{extra_vals} "
            f"FROM (VALUES {', '.join(rows_sql)}) "
            f"     AS v({', '.join(columns)}) "
            f"JOIN results r ON r.id = v.result_id AND r.user_id = v.user_id "
            f"WHERE r.user_id = CAST(:uid AS uuid) "
            f"  AND r.skip_trace_status = :claimable "
            f"ON CONFLICT DO NOTHING "
            f"RETURNING result_id"
        ),
        params,
    ).scalars().all()
    claimed_ids = [str(r) for r in claimed]
    if not claimed_ids:
        return []

    # Advance ONLY the rows the insert won. The status predicate is repeated
    # because the insert's join and this update are separate statements.
    db.execute(
        text(
            "UPDATE results SET skip_trace_status = 'queued' "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND user_id = CAST(:uid AS uuid) "
            "  AND skip_trace_status = :claimable"
        ),
        {"ids": sorted(claimed_ids), "uid": user_id,
         "claimable": CLAIMABLE_RESULT_STATUS},
    )
    return claimed_ids
