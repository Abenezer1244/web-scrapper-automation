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

KNOWN GAP, NOT FIXED HERE (raised by Codex, then found to be worse than raised).
`skip_trace_attempted_at` means "last ATTEMPT", not "when we obtained this data",
and three sites stamp it to now() while acquiring NOTHING:
skip_trace_dispatcher.py:569, :880 and tracerfy_ingest.py:782, all on the
'errored' transition. So a row holding 400-day-old contact data that is re-traced
and errors has its retention clock reset to today, and that old PII gets a fresh
full window. Re-queueing itself does not stamp the column (enrich.py:2146), which
is why the in-flight guard above is safe -- but the error path does.

The honest fix is a dedicated "PII obtained at" column that only the write paths
that actually store contact data set, with the purge aging off that. That is a
migration plus edits to the PAID ingest path, and it is deliberately NOT done
blind: nothing in this change has run against a real database (pytest is banned
locally, CI is billing-blocked). It also overlaps decision D1, which is already
with counsel. Tracked in tasks/todo-retention-purge.md §D.

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
# "has any contact data" guard skips rows whose four contact columns are all NULL.
# Note it tests non-NULL, not non-empty: a 'miss' is stored with phones/emails = []
# (an encrypted empty list, non-NULL), so an aged miss IS swept to 'purged', and so
# is an 'errored' or unknown-status row still holding older data. Whether misses
# should be swept is an open owner decision (BUILD_JOURNAL 2026-10-01, UX 2e).
# Without the two guards the task would
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

# In-flight rows that are ALREADY past retention. Excluding them from the sweep is
# correct (see _ELIGIBLE), but "correct" must not mean "invisible": if the
# dispatcher never settles a claim, the row's old PII sits here exempt, and a
# silent permanent exemption in a retention task is exactly the failure this whole
# job exists to prevent. Counted every run and logged when non-zero. A number that
# does not fall between runs means the dispatcher is stuck, not that we are done.
_COUNT_INFLIGHT = text(
    "SELECT count(*) FROM results "
    "WHERE skip_trace_attempted_at < :cutoff "
    "  AND skip_trace_status IN ('queued', 'submitted') "
    "  AND (phone IS NOT NULL OR email IS NOT NULL "
    "       OR phones IS NOT NULL OR emails IS NOT NULL)"
)

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
_CLEAR_EXPORT = text(
    "UPDATE jobs SET export_key = NULL WHERE id = :id AND export_key = :key"
)

_AGED_BATCH_EXPORTS = text(
    "SELECT id, combined_export_key FROM batch_runs "
    "WHERE combined_export_key IS NOT NULL AND created_at < :cutoff "
    "ORDER BY created_at LIMIT :batch"
)
_CLEAR_BATCH_EXPORT = text(
    "UPDATE batch_runs SET combined_export_key = NULL "
    "WHERE id = :id AND combined_export_key = :key"
)

# Tracerfy completion links. Not a copy of the PII, but a live ACCESS PATH to it:
# the CDN needs no auth, so the URL alone fetches a CSV of traced phone numbers.
# Two windows, because the link is retained for a reason. A 'completed' queue was
# already ingested and has nothing left to recover, so its link goes at the short
# window. A 'pending'/'errored' queue may still be recovered by hand
# (tracerfy_ingest.py:350) -- but that recovery is only meaningful while the data
# is still retainable, so those go at the PII window. Beyond it there is nothing
# legitimate left to do with a link to data we are simultaneously deleting.
#
# `download_url IS NOT NULL` keeps it idempotent. No new grant: the system role's
# blanket UPDATE already covers this.
# Both branches name their status explicitly (Codex, High). The long-window branch
# was previously unconstrained, so it applied to EVERY status, including any added
# later -- a future in-flight state would have had its link cleared by a rule that
# never mentioned it. This table's statuses are pending|completed|errored
# (models.py:1201); anything else is new and should not be silently swept.
#
# The completed branch keys off completed_at ALONE, not COALESCE. Falling back to
# submitted_at would clear a completed row early whenever completed_at is missing,
# and "deleted sooner than the rule says" is still the wrong answer even when the
# direction is safe. A completed row with no completed_at is a data-integrity
# anomaly; it is counted and reported rather than guessed at.
_LINK_ELIGIBLE = (
    "download_url IS NOT NULL "
    "AND ( (status = 'completed' AND completed_at < :link_cutoff) "
    "   OR (status IN ('pending', 'errored') "
    "       AND COALESCE(completed_at, submitted_at) < :pii_cutoff) )"
)

# A completed queue whose completed_at never got written. It cannot age out of the
# short window, so its no-auth CDN link would sit there until the PII window, or
# forever if the status set ever changes. Surfaced, never silently swept.
_COUNT_LINK_ANOMALIES = text(
    "SELECT count(*) FROM skip_trace_queues "
    "WHERE download_url IS NOT NULL AND status = 'completed' AND completed_at IS NULL"
)

_PURGE_LINKS = text(
    f"UPDATE skip_trace_queues SET download_url = NULL WHERE id IN ("
    f"  SELECT id FROM skip_trace_queues WHERE {_LINK_ELIGIBLE} "
    "  ORDER BY COALESCE(completed_at, submitted_at) "
    "  LIMIT :batch FOR UPDATE SKIP LOCKED)"
)

_COUNT_LINKS = text(f"SELECT count(*) FROM skip_trace_queues WHERE {_LINK_ELIGIBLE}")


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _apply_timeouts(db) -> None:
    """Bound lock wait + statement time for the current transaction."""
    db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    db.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))


def _drain(db, stmt, params: dict, batch: int) -> tuple[int, bool]:
    """Run `stmt` in bounded batches until it stops matching rows.

    Returns (rows affected, hit_cap).

    Stops on an EMPTY batch, not a short one (Codex, Medium). FOR UPDATE SKIP
    LOCKED returns short whenever another transaction holds some of the rows, so
    treating "short" as "finished" would abandon eligible rows the moment the
    dispatcher touched a few of them -- and on a compliance sweep, quietly
    stopping early is the worst available behaviour. A zero batch still does not
    prove completion (everything remaining could be locked), which is why the
    caller re-counts afterwards instead of trusting this loop.
    """
    total = 0
    for _ in range(_MAX_BATCHES):
        _apply_timeouts(db)
        result = db.execute(stmt, {**params, "batch": batch})
        db.commit()
        count = result.rowcount or 0
        total += count
        if count == 0:
            return total, False
    return total, True


def _sweep_exports(db, stmt, clear_stmt, cutoff: datetime, batch: int) -> tuple[int, int]:
    """Delete aged R2 export objects, clearing the key only once the object is gone.

    Order matters and is deliberate: delete the object FIRST, and NULL the column
    only on success. The reverse would lose the key while the file stayed in R2,
    leaving an orphaned copy of someone's phone number that nothing can ever find
    again. A failed delete keeps the key so the next run retries it.

    The clear is CONDITIONAL on the key we actually deleted (Codex, High). There is
    a commit and a network round trip between selecting a row and clearing it, and
    a job can be re-exported in that gap. An unconditional `SET export_key = NULL
    WHERE id = :id` would then wipe the NEW key while its file sat in R2 -- the
    exact orphan this ordering exists to prevent, reintroduced by the last line.
    A zero rowcount means the key moved on and this row is simply left for the
    next run.

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
        cleared = db.execute(clear_stmt, {"id": row_id, "key": key})
        db.commit()
        if not (cleared.rowcount or 0):
            _logger.info(
                "retention: export key on row %s changed while we were deleting it; "
                "the object is gone, leaving the new key for a later run", row_id,
            )
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
    link_cutoff = _cutoff(settings.SKIP_TRACE_LINK_RETENTION_DAYS)
    link_params = {"link_cutoff": link_cutoff, "pii_cutoff": results_cutoff}
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
            links = db.execute(_COUNT_LINKS, link_params).scalar_one()
            db.commit()
            _logger.warning(
                "retention purge DRY RUN (nothing written): would clear PII on %d "
                "results rows attempted before %s, delete %d skip_trace_cache rows "
                "fetched before %s, clear %d provider download links, and delete %d "
                "job + %d batch export objects from R2 older than %s. "
                "Set RETENTION_PURGE_DRY_RUN=false to enforce.",
                eligible, results_cutoff.date(), cache_eligible, cache_cutoff.date(),
                links, exports, batch_exports, export_cutoff.date(),
            )
            return

        rows, rows_capped = _drain(
            db, _PURGE_RESULTS, {"cutoff": results_cutoff, "purged": purged}, batch
        )
        cache_rows, cache_capped = _drain(
            db, _PURGE_CACHE, {"cutoff": cache_cutoff}, batch
        )
        link_rows, _ = _drain(db, _PURGE_LINKS, link_params, batch)
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
        inflight = db.execute(_COUNT_INFLIGHT, {"cutoff": results_cutoff}).scalar_one()
        link_anomalies = db.execute(_COUNT_LINK_ANOMALIES).scalar_one()
        db.commit()

    elapsed = (datetime.now(UTC) - started).total_seconds()
    _logger.info(
        "retention purge: cleared PII on %d results rows, deleted %d "
        "skip_trace_cache rows, cleared %d provider download links, deleted %d job "
        "+ %d batch export objects in %.1fs",
        rows, cache_rows, link_rows, exports_deleted, batch_deleted, elapsed,
    )
    if inflight:
        _logger.warning(
            "retention purge: %d results rows are past retention but sat in "
            "queued/submitted and were skipped to avoid discarding a paid trace. "
            "Expected to clear on a later run. If this count does NOT fall, the "
            "dispatcher is not settling claims and that PII is exempt indefinitely.",
            inflight,
        )
    if link_anomalies:
        _logger.error(
            "retention purge: %d skip_trace_queues rows are 'completed' with NO "
            "completed_at and still hold a provider download link. They cannot age "
            "out of the short window, and that link needs no auth to fetch a CSV of "
            "traced numbers. Investigate rather than widening the predicate.",
            link_anomalies,
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
