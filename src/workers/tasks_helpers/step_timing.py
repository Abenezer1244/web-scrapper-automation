"""Wall time and database round trips for one step of a scrape job (worker log only).

A King tax re-run spent minutes in steps that logged nothing (2026-10-12, job
0a32375e: 6.7 min between "237 records have no deliverable address" and "owner
flags recomputed"), and the per-step costs could only be guessed from the gaps
between job_logs lines. Each step wrapped here logs how long it took and how
many statements it sent, so a slow step shows whether it waits on the database
(one round trip per row) or on something else.

Diagnostics only: nothing here changes what a step does, and a failure to
measure never fails the step.
"""

from contextlib import contextmanager
import time

from sqlalchemy import event

from src.utils.logger import setup_logger

_logger = setup_logger("worker.task")


@contextmanager
def timed_step(db, job_id: str, step: str):
    """Log `step`'s elapsed seconds, statements and parameter sets on exit.

    Counted on the session's engine. Under psycopg2 an executemany sends one
    round trip per parameter set, so `param_sets` is the round-trip count. The
    worker runs one task per process (prefork), so the engine's statements in
    the window are this job's own; the heartbeat uses a separate engine. The
    line is logged on success and on failure alike, a failing step being the
    one most worth timing."""
    counts = {"statements": 0, "param_sets": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        counts["statements"] += 1
        counts["param_sets"] += len(parameters) if executemany and parameters else 1

    engine = None
    try:
        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", _count)
    except Exception:  # noqa: BLE001 -- measuring must never fail the step
        engine = None
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        if engine is not None:
            try:
                event.remove(engine, "before_cursor_execute", _count)
            except Exception:  # noqa: BLE001 -- measuring must never fail the step
                pass
        _logger.info(
            "Job %s: step %s took %.1fs (%d statements, %d parameter sets)",
            job_id, step, elapsed, counts["statements"], counts["param_sets"],
        )
