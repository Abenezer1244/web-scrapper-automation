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
}

_EXACT: dict[str, int] = {"state": 2, "mail_state": 2}

# `trace_type` is VALIDATED, never truncated. Truncating it to its 16-character
# column could turn a bad value into a row the dispatcher never drains: it
# batches strictly by 'normal' / 'advanced', so anything else sits queued
# forever, counted as in progress and never submitted or settled.
_TRACE_TYPES = frozenset({"normal", "advanced"})


class ClaimUnenforcedError(RuntimeError):
    """Migration 099's index is absent, so a second active claim is possible.

    Raised only for callers that pass ``require_enforcement=True`` -- the ones
    that would be a SECOND writer of the queue and so need the database to
    arbitrate. See `claim_skip_trace_rows`.
    """


def claim_enforcement_ok(db) -> bool:
    """True when migration 099's index is present, valid, and the RIGHT index.

    Checked by identity, not by name: `CREATE INDEX IF NOT EXISTS` would happily
    accept a same-named index on another table or with a wider predicate, and
    either one enforces something other than "one active claim per lead" while
    looking applied. So this asserts the table, uniqueness, validity, the indexed
    column, and that the predicate names exactly ACTIVE_PENDING_STATUSES and
    nothing else.
    """
    row = db.execute(
        text(
            "SELECT i.indisunique, i.indisvalid, "
            "       pg_get_expr(i.indpred, i.indrelid) AS predicate, "
            "       pg_get_indexdef(i.indexrelid) AS definition "
            "FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "WHERE c.relname = :n AND t.relname = 'pending_skip_trace_rows'"
        ),
        {"n": INDEX_NAME},
    ).first()
    if row is None or not row.indisunique or not row.indisvalid:
        return False
    if "(result_id)" not in (row.definition or ""):
        return False
    predicate = row.predicate or ""
    if any(f"'{s}'" not in predicate for s in ACTIVE_PENDING_STATUSES):
        return False
    # Nothing EXTRA: a widened predicate would refuse legitimate re-claims of a
    # lead whose earlier row is genuinely finished.
    return predicate.count("::character varying") == len(ACTIVE_PENDING_STATUSES)


def warn_if_unenforced(db) -> bool:
    """`claim_enforcement_ok`, but says so out loud when the answer is no."""
    enforced = claim_enforcement_ok(db)
    if not enforced:
        _logger.error(
            "Skip-trace claim is running UNENFORCED: %s is missing, invalid or not "
            "the expected index, so nothing stops a second active claim for one "
            "lead and a lead can be charged for twice. Apply migration 099.",
            INDEX_NAME,
        )
    return enforced


def _truncate(column: str, value: Any) -> Any:
    width = _WIDTHS.get(column)
    if width is None or value is None:
        return value
    text_value = str(value)
    return text_value[:width] if len(text_value) > width else text_value


def _is_writable(payload: dict) -> bool:
    """False for a payload that cannot be stored without changing its meaning."""
    for column, width in _EXACT.items():
        value = payload.get(column)
        if value is not None and len(str(value)) > width:
            return False
    return payload.get("trace_type") in _TRACE_TYPES


def claim_skip_trace_rows(
    db,
    payloads: list[dict],
    *,
    require_enforcement: bool = True,
) -> list[str]:
    """Claim `payloads` into the queue. Returns the result ids actually won.

    `payloads` are ``build_pending_row_payload`` dicts. A payload whose lead no
    longer exists, belongs to another tenant, or is no longer 'not_attempted' is
    simply not claimed -- the insert filters it, so it can neither fail the batch
    nor strand an active row nobody will settle.

    `require_enforcement` decides what happens when migration 099's index is
    absent, and the right answer differs by caller (Codex round 15 diff review,
    round 2). The database is what stops one lead being claimed, and charged
    for, twice; an error log is observability, not enforcement.

    * The SCRAPE enqueue passes False. It is the ONLY writer of this queue
      today, which is exactly the situation before 099 existed, and in that
      situation a missing index risks nothing that is not already true -- while
      refusing to claim would strand every lookup in the product because
      `start.sh` deliberately starts the worker when migrations fail.
    * The ACTION worker (Phase 1b-2) passes True, the default. It is the SECOND
      writer, so without the index two writers really can buy the same lead
      twice. It must refuse rather than risk a customer's money, and refusing
      costs only that one action, which reports a clean failure and charges
      nothing.

    Does NOT commit. The caller owns the transaction.
    """
    if not payloads:
        return []

    if require_enforcement and not claim_enforcement_ok(db):
        raise ClaimUnenforcedError(
            f"{INDEX_NAME} is missing, invalid or not the expected index; refusing "
            "to claim, because nothing would stop this lead being charged for "
            "twice. Apply migration 099."
        )

    # One claim is one tenant's work: the single tenant-scoped UPDATE that
    # advances `results` would otherwise be too broad or silently partial.
    user_ids = {str(p["user_id"]) for p in payloads}
    if len(user_ids) != 1:
        raise ValueError(
            f"claim_skip_trace_rows: expected one user_id, got {len(user_ids)}"
        )
    user_id = next(iter(user_ids))

    # Refuse rather than mangle: a value whose column cannot hold it (_EXACT) or
    # a trace_type the dispatcher would never drain.
    usable, refused = [], []
    for payload in payloads:
        (usable if _is_writable(payload) else refused).append(payload)
    if refused:
        # Result ids only: never the homeowner's name or address in a log line.
        _logger.warning(
            "Skip-trace claim refused %d payload(s) with an unusable state or "
            "trace_type; result ids: %s",
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
            f"INSERT INTO pending_skip_trace_rows ({', '.join(columns)}, status) "  # noqa: S608
            f"SELECT {select_list}, 'queued' "
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
    #
    # There is a narrow interleaving where a concurrent writer settles a lead
    # BETWEEN the insert and this update, so the pending row is active while
    # `results` reads 'hit'. It is deliberately not locked against, because it
    # already self-heals and never reaches the vendor: the dispatcher's
    # `_cancel_undeliverable_queued` cancels any queued row whose result is no
    # longer 'queued' (the `r.skip_trace_status <> 'queued'` arm), on the next
    # five-minute tick, before the submit loop. Locking `results` first to close
    # it would invert this module's lock order against that same sweep.
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
