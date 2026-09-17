"""Body logic for the skip-trace PII retention beat task (Privacy Policy §7).

  * `_purge_skip_trace_pii_impl` — NULLs vendor-sourced contact PII on `results`
    rows past the retention window, and deletes aged `skip_trace_cache` rows.

Policy §7 promises 365-day deletion of lead records. The owner's decision is to
keep the lead row — parcel, situs, notice date, owner name, all of it county
PUBLIC RECORD — and delete the part that is actually personal and actually
purchased: the phone/email Tracerfy returned. See tasks/todo-retention-purge.md.

WHAT THIS DOES NOT DO. Delivered CSV/XLSX exports in R2 still contain the same
phone numbers, and nothing here reaches them. Until the export sweep and/or the
R2 lifecycle rule are in place, the database being clean does NOT mean the
promise is kept.

THE CLOCK IS A POLICY QUESTION, NOT AN ENGINEERING ONE. We age rows off
`skip_trace_attempted_at`, i.e. "retain each newly obtained copy for N days".
That is NOT the same as "delete N days after the lead was created": a row that
is re-traced (or refreshed from a cache hit — enrich.py stamps the column there
too) resets its own clock and can stay populated indefinitely. That is correct
under the first reading and wrong under the second. It is with counsel; if the
answer is the second reading, change `_ELIGIBLE` to key off `created_at`.

Ships behind RETENTION_PURGE_ENABLED (off) and RETENTION_PURGE_DRY_RUN (on)
because the deletion is IRREVERSIBLE and the clock question is open. Dry run
logs exactly what it would purge and writes nothing.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.config import settings
from src.config.constants import SkipTraceStatus
from src.db.session import system_sync_session
from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

# Bound each statement so a purge can never become the thing that takes the site
# down: it is maintenance, and it must always lose to live traffic.
_LOCK_TIMEOUT = "5s"
_STATEMENT_TIMEOUT = "60s"

# Hard stop on batches per run. At the default batch size this is 200k rows of
# `results` per run; a backlog larger than that drains over subsequent runs
# rather than holding one worker for an unbounded time.
_MAX_BATCHES = 200

# The six columns Tracerfy populates. `phone_type` and `phone_dnc_flag` are
# metadata ABOUT the number, so they go with it — a DNC flag for a number we no
# longer hold is both useless and still a statement about a person.
_PII_COLUMNS = (
    "phone = NULL",
    "phone_type = NULL",
    "phone_dnc_flag = NULL",
    "email = NULL",
    "phones = NULL",
    "emails = NULL",
)

# Eligibility. Two guards make this idempotent, which matters because the sweep
# runs daily forever: the status guard stops a purged row matching again, and the
# "has any contact data" guard means rows that missed or errored (attempted, but
# no PII ever returned) are never touched at all. Without them the task would
# re-UPDATE already-NULL rows every single day, generating dead tuples forever
# for no reason.
#
# IN-FLIGHT ROWS ARE EXCLUDED, and this one is not theoretical. A row that was
# traced long ago and has since been RE-QUEUED carries old PII (past retention)
# while sitting in queued/submitted. Purging it would flip its status to 'purged',
# and tracerfy_ingest.py:780 only accepts a provider result for a row still
# IN ('queued','submitted') -- so the callback would match nothing and a lookup we
# PAID for would be silently discarded. Retention must never race the dispatcher.
# These rows purge on a later run, once the trace lands and the clock still says
# they are due.
_ELIGIBLE = (
    "skip_trace_attempted_at < :cutoff "
    "AND skip_trace_status <> :purged "
    "AND skip_trace_status NOT IN ('queued', 'submitted') "
    "AND (phone IS NOT NULL OR email IS NOT NULL "
    "     OR phones IS NOT NULL OR emails IS NOT NULL)"
)

_PURGE_RESULTS = text(
    f"UPDATE results SET {', '.join(_PII_COLUMNS)}, skip_trace_status = :purged "
    "WHERE id IN ("
    f"  SELECT id FROM results WHERE {_ELIGIBLE} "
    "  ORDER BY skip_trace_attempted_at, id "
    "  LIMIT :batch FOR UPDATE SKIP LOCKED)"
)

_COUNT_RESULTS = text(f"SELECT count(*) FROM results WHERE {_ELIGIBLE}")

_OLDEST_RESULT = text(
    f"SELECT min(skip_trace_attempted_at) FROM results WHERE {_ELIGIBLE}"
)

# The cache is keyed by a hash of (user_id, address), and `raw_response` holds the
# FULL Tracerfy payload, so these rows are the densest vendor PII we store. They
# are also dead weight: enrich.py's TTL check is read-time only and never deletes,
# so a row past SKIP_TRACE_CACHE_DAYS can never be used as a cache hit again. We
# age them off the reuse window rather than the 365-day figure, because retaining
# an unusable row full of someone's contact details buys nothing.
_PURGE_CACHE = text(
    "DELETE FROM skip_trace_cache WHERE address_hash IN ("
    "  SELECT address_hash FROM skip_trace_cache WHERE fetched_at < :cutoff "
    "  ORDER BY fetched_at LIMIT :batch FOR UPDATE SKIP LOCKED)"
)

_COUNT_CACHE = text("SELECT count(*) FROM skip_trace_cache WHERE fetched_at < :cutoff")

# Delivered exports. These are the copies that matter most and are easiest to
# forget: a CSV in R2 holds the same phone numbers as the row, is reachable by
# anyone with a signed link, and NOTHING has ever deleted one. Both key columns
# are swept; a job's own export and a batch run's combined CSV are equally a copy.
# We age off finished_at (when the file was produced), falling back to created_at
# for a job that never recorded a finish.
_AGED_EXPORTS = text(
    "SELECT id, export_key FROM jobs "
    "WHERE export_key IS NOT NULL "
    "  AND COALESCE(finished_at, created_at) < :cutoff "
    "ORDER BY COALESCE(finished_at, created_at) LIMIT :batch"
)
_CLEAR_EXPORT = text("UPDATE jobs SET export_key = NULL WHERE id = :id")

_AGED_BATCH_EXPORTS = text(
    "SELECT id, combined_export_key FROM batch_runs "
    "WHERE combined_export_key IS NOT NULL AND created_at < :cutoff "
    "ORDER BY created_at LIMIT :batch"
)
_CLEAR_BATCH_EXPORT = text(
    "UPDATE batch_runs SET combined_export_key = NULL WHERE id = :id"
)


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _apply_timeouts(db) -> None:
    """Bound lock wait + statement time for the current transaction."""
    db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    db.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))


def _drain(db, stmt, params: dict, batch: int) -> tuple[int, bool]:
    """Run `stmt` in bounded batches until it stops matching rows.

    Returns (rows affected, hit_cap). A short batch does NOT reliably mean
    "finished": FOR UPDATE SKIP LOCKED also returns short when another
    transaction holds the rows, so the caller re-counts what is left rather than
    trusting this loop to have drained everything.
    """
    total = 0
    for _ in range(_MAX_BATCHES):
        _apply_timeouts(db)
        result = db.execute(stmt, {**params, "batch": batch})
        db.commit()
        count = result.rowcount or 0
        total += count
        if count < batch:
            return total, False
    return total, True


def _sweep_exports(db, stmt, clear_stmt, cutoff: datetime, batch: int) -> tuple[int, int]:
    """Delete aged R2 export objects, clearing the key only once the object is gone.

    Order matters and is deliberate: delete the object FIRST, and NULL the column
    only on success. The reverse would lose the key while the file stayed in R2,
    leaving an orphaned copy of someone's phone number that nothing can ever find
    again. A failed delete keeps the key so the next run retries it.

    Returns (objects deleted, delete failures).
    """
    from src.utils.data_exporter import DataExporter

    exporter = DataExporter()
    deleted = failed = 0
    _apply_timeouts(db)
    rows = db.execute(stmt, {"cutoff": cutoff, "batch": batch}).all()
    db.commit()
    for row_id, key in rows:
        try:
            ok = exporter.delete_from_r2(key)
        except Exception:
            _logger.exception("retention: R2 delete raised for key on row %s", row_id)
            ok = False
        if not ok:
            failed += 1
            continue
        _apply_timeouts(db)
        db.execute(clear_stmt, {"id": row_id})
        db.commit()
        deleted += 1
    return deleted, failed


def _purge_skip_trace_pii_impl() -> None:
    """Purge aged skip-trace PII from `results` and `skip_trace_cache`. Daily."""
    if not settings.RETENTION_PURGE_ENABLED:
        return

    batch = settings.RETENTION_PURGE_BATCH
    purged = SkipTraceStatus.PURGED.value
    results_cutoff = _cutoff(settings.SKIP_TRACE_PII_RETENTION_DAYS)
    cache_cutoff = _cutoff(settings.SKIP_TRACE_CACHE_RETENTION_DAYS)
    export_cutoff = _cutoff(settings.EXPORT_RETENTION_DAYS)
    started = datetime.now(UTC)

    # Cross-tenant by design: retention is an obligation we owe regardless of
    # whose tenant a row sits in, and there is no request user to scope to. No
    # value below comes from user input; both cutoffs are computed here.
    with system_sync_session() as db:
        if settings.RETENTION_PURGE_DRY_RUN:
            _apply_timeouts(db)
            eligible = db.execute(
                _COUNT_RESULTS, {"cutoff": results_cutoff, "purged": purged}
            ).scalar_one()
            cache_eligible = db.execute(
                _COUNT_CACHE, {"cutoff": cache_cutoff}
            ).scalar_one()
            exports = db.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE export_key IS NOT NULL "
                    "AND COALESCE(finished_at, created_at) < :cutoff"
                ),
                {"cutoff": export_cutoff},
            ).scalar_one()
            batch_exports = db.execute(
                text(
                    "SELECT count(*) FROM batch_runs WHERE combined_export_key IS NOT NULL "
                    "AND created_at < :cutoff"
                ),
                {"cutoff": export_cutoff},
            ).scalar_one()
            db.commit()
            _logger.warning(
                "retention purge DRY RUN (nothing written): would clear PII on %d "
                "results rows attempted before %s, delete %d skip_trace_cache rows "
                "fetched before %s, and delete %d job + %d batch export objects from "
                "R2 older than %s. Set RETENTION_PURGE_DRY_RUN=false to enforce.",
                eligible, results_cutoff.date(), cache_eligible, cache_cutoff.date(),
                exports, batch_exports, export_cutoff.date(),
            )
            return

        rows, rows_capped = _drain(
            db, _PURGE_RESULTS, {"cutoff": results_cutoff, "purged": purged}, batch
        )
        cache_rows, cache_capped = _drain(
            db, _PURGE_CACHE, {"cutoff": cache_cutoff}, batch
        )
        exports_deleted, export_failures = _sweep_exports(
            db, _AGED_EXPORTS, _CLEAR_EXPORT, export_cutoff, batch
        )
        batch_deleted, batch_failures = _sweep_exports(
            db, _AGED_BATCH_EXPORTS, _CLEAR_BATCH_EXPORT, export_cutoff, batch
        )

        # What is LEFT is the number that matters for a compliance promise. A
        # non-zero remainder means rows are past their retention window and still
        # hold PII right now, whether that is lock contention or the batch cap.
        _apply_timeouts(db)
        remaining = db.execute(
            _COUNT_RESULTS, {"cutoff": results_cutoff, "purged": purged}
        ).scalar_one()
        oldest = db.execute(
            _OLDEST_RESULT, {"cutoff": results_cutoff, "purged": purged}
        ).scalar()
        db.commit()

    elapsed = (datetime.now(UTC) - started).total_seconds()
    _logger.info(
        "retention purge: cleared PII on %d results rows, deleted %d "
        "skip_trace_cache rows, deleted %d job + %d batch export objects in %.1fs",
        rows, cache_rows, exports_deleted, batch_deleted, elapsed,
    )
    if export_failures or batch_failures:
        _logger.error(
            "retention purge: %d job + %d batch export objects could NOT be deleted "
            "from R2. Their keys were left in place so the next run retries them, but "
            "until then those files still hold contact PII past its retention window.",
            export_failures, batch_failures,
        )
    if remaining:
        _logger.error(
            "retention purge INCOMPLETE: %d results rows are past retention and "
            "still hold PII (oldest attempted %s; batch cap hit: results=%s "
            "cache=%s). This is an open compliance gap until the next run clears "
            "it -- if it persists across runs, investigate lock contention.",
            remaining, oldest, rows_capped, cache_capped,
        )
