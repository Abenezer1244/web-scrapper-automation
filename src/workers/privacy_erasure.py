"""The privacy erasure driver (beat): carries each deletion rule's erasure to completion.

Design section 7 ("run at activation and again after in-flight jobs drain"), plan
tasks/todo-privacy-phase6-p6.md P6c/P6d. One privacy_erasures row per rule (migration
135); P7's activation inserts it. Until then the table is empty and a tick is one query.

Phases, each resumable (markers and the scan cursor live on the row):
  first pass   every store, src/privacy/erase.py, one committed step at a time.
  R2 sweep     the exports that named the subject: job exports and batch combined CSVs
               marked invalidated during this erasure, and their owners' ready
               account-export ZIPs requested before the pass (set 'expired'; the owner
               can request a new one). Selection is by time since the erasure began,
               so it can also delete copies another erasure marked: only stale copies,
               never anything a download needs.
               Every download renders from the database, which already excludes the
               rows, so nothing a customer can reach breaks; only stale copies go. A
               delete that fails raises: the sweep is never marked done on an error.
  second pass  at least 24 h after the first, and only once no job or batch run
               started before it is still running. If it erases anything (an in-flight write landed), the
               erasure goes round again: new R2 sweep, new second pass 24 h later.
  final sweep  the R2 sweep again, after everything older than the first pass has
  + complete   finished (a late upload built from pre-erasure data exists by then);
               complete once it succeeds and no lookup of an erased lead is still out
               at Tracerfy.

One driver at a time: a session advisory lock held on its own connection for the
whole tick (as account_deletion_beat). A failure is recorded on the row (the
exception's class name only, never data) and retried next tick; other rules proceed.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.privacy.erase import erase_step, load_erasure_rule
from src.utils.logger import setup_logger

_logger = setup_logger("worker.privacy_erasure")

_LOCK_KEY = 7_135_000_001
_TICK_BUDGET_S = 240.0
SECOND_PASS_DELAY = timedelta(hours=24)

_OPEN_SQL = text(
    "SELECT rule_id::text AS rule_id, rule_row_id::text AS rule_row, created_at, "
    "first_pass_at, r2_swept_at, second_pass_at, scan_cursor, counts "
    "FROM privacy_erasures WHERE completed_at IS NULL ORDER BY created_at"
)
# Anything started before the first pass may still upload an export built from data
# read before it: the second pass and the final sweep wait for all of it.
_IN_FLIGHT_SQL = text(
    "SELECT 1 FROM jobs WHERE created_at < :before "
    "AND status NOT IN ('done', 'failed', 'cancelled') "
    "UNION ALL SELECT 1 FROM batch_runs WHERE created_at < :before "
    "AND status IN ('pending', 'running') LIMIT 1"
)
_OUT_AT_TRACERFY_SQL = text(
    "SELECT 1 FROM pending_skip_trace_rows p JOIN results r ON r.id = p.result_id "
    "WHERE r.privacy_erased_at IS NOT NULL AND p.status IN ('submitting', 'submitted') "
    "LIMIT 1"
)
_EXPORT_KEYS_SQL = text(
    "SELECT export_key FROM jobs WHERE privacy_invalidated_at >= :since "
    "AND export_key IS NOT NULL "
    "UNION SELECT combined_export_key FROM batch_runs WHERE privacy_invalidated_at >= :since "
    "AND combined_export_key IS NOT NULL"
)
_OWNERS_SQL = (
    "SELECT DISTINCT user_id FROM jobs WHERE privacy_invalidated_at >= :since "
    "UNION SELECT DISTINCT user_id FROM batch_runs WHERE privacy_invalidated_at >= :since"
)
# Account ZIPs requested before the latest pass could hold the subject; later ones are
# rendered from the already-erased rows.
_EXPORTS_BUILDING_SQL = text(
    f"SELECT 1 FROM account_exports WHERE status IN ('pending', 'building') "  # noqa: S608
    f"AND requested_at < :until AND user_id IN ({_OWNERS_SQL}) LIMIT 1"  # constant SQL
)
_EXPORTS_READY_SQL = text(
    f"SELECT id::text AS id, user_id::text AS user_id FROM account_exports "  # noqa: S608
    f"WHERE status = 'ready' AND requested_at < :until "
    f"AND user_id IN ({_OWNERS_SQL})"  # constant SQL only
)


class R2Store:
    """Export objects in R2. Tests pass an in-memory stand-in."""

    def delete(self, key: str) -> bool:
        from src.utils.data_exporter import DataExporter
        return DataExporter().delete_from_r2(key)


class _OutOfTimeError(Exception):
    pass


def _merge(counts: dict, step: dict) -> dict:
    out = dict(counts)
    for k, v in step.items():
        out[k] = out.get(k, 0) + v
    return out


def _run_pass(db, row, rule, deadline: float, counts_key: str) -> tuple[bool, int]:
    """Steps until the pass ends (True) or time runs out (False). Each step commits with
    its cursor and counts, so a stop anywhere resumes there. Returns (finished, the
    number of things this pass erased so far, across ticks)."""
    cursor = dict(row.scan_cursor or {})
    counts = dict(row.counts or {})
    while time.monotonic() < deadline:
        step = erase_step(db, rule, cursor)
        counts = _merge(counts, step.counts)
        erased = sum(step.counts.values())
        if erased:
            counts[counts_key] = counts.get(counts_key, 0) + erased
        cursor = step.cursor
        db.execute(text(
            "UPDATE privacy_erasures SET scan_cursor = CAST(:c AS jsonb), "
            "counts = CAST(:n AS jsonb) WHERE rule_id = CAST(:r AS uuid)"),
            {"c": _json(cursor), "n": _json(counts), "r": row.rule_id})
        db.commit()
        if step.done:
            return True, counts.get(counts_key, 0)
    return False, counts.get(counts_key, 0)


def _json(value) -> str:
    import json
    return json.dumps(value)


def _sweep_r2(db, row, store: R2Store, deadline: float) -> bool:
    """Delete this erasure's export copies. False = wait (an account export is still
    being built for an affected owner, so it could hold the subject)."""
    since = {"since": row.created_at, "until": row.first_pass_at}
    if db.execute(_EXPORTS_BUILDING_SQL, since).first():
        db.rollback()
        return False
    keys = list(db.execute(_EXPORT_KEYS_SQL, since).scalars())
    ready = db.execute(_EXPORTS_READY_SQL, since).all()
    db.rollback()
    from src.workers.account_export import export_key

    for key in keys:
        if time.monotonic() >= deadline:
            raise _OutOfTimeError
        if not store.delete(key):
            raise RuntimeError("R2 delete failed")
    for export in ready:
        if time.monotonic() >= deadline:
            raise _OutOfTimeError
        if not store.delete(export_key(export.user_id, export.id)):
            raise RuntimeError("R2 delete failed")
        db.execute(text("UPDATE account_exports SET status = 'expired' "
                        "WHERE id = CAST(:i AS uuid) AND status = 'ready'"), {"i": export.id})
        db.commit()
    return True


def _older_work_running(db, row) -> bool:
    running = db.execute(_IN_FLIGHT_SQL, {"before": row.first_pass_at}).first() is not None
    db.rollback()
    return running


def _set(db, rule_id: str, assignments: str, now: datetime) -> None:
    """Phase markers take the tick's clock (:now), the same one the phase decisions
    read, never the database's own now()."""
    db.execute(text(f"UPDATE privacy_erasures SET {assignments} "  # noqa: S608 -- constants
                    "WHERE rule_id = CAST(:r AS uuid)"), {"r": rule_id, "now": now})
    db.commit()


def _advance(db, row, store: R2Store, deadline: float, now: datetime) -> str:
    """Move one erasure forward as far as the time allows. Returns its phase."""
    rule = load_erasure_rule(db, row.rule_id, row.rule_row)
    db.rollback()  # the snapshot read holds nothing; every write below commits itself
    if row.first_pass_at is None:
        finished, _ = _run_pass(db, row, rule, deadline, "first_pass_erased")
        if not finished:
            return "first_pass"
        _set(db, row.rule_id, "first_pass_at = :now, scan_cursor = '{}'::jsonb", now)
        return "first_pass_done"
    if row.r2_swept_at is None:
        if not _sweep_r2(db, row, store, deadline):
            return "r2_waiting"
        _set(db, row.rule_id, "r2_swept_at = :now", now)
        return "r2_swept"
    if row.second_pass_at is None:
        if now < row.first_pass_at + SECOND_PASS_DELAY or _older_work_running(db, row):
            return "second_pass_waiting"
        finished, erased = _run_pass(db, row, rule, deadline, "second_pass_erased")
        if not finished:
            return "second_pass"
        if erased:
            # An in-flight write landed after the first pass: sweep the exports again and
            # wait another 24 h before checking once more.
            _set(db, row.rule_id,
                 "first_pass_at = :now, r2_swept_at = NULL, scan_cursor = '{}'::jsonb, "
                 "counts = counts - 'second_pass_erased'", now)
            return "repeat"
        _set(db, row.rule_id, "second_pass_at = :now, scan_cursor = '{}'::jsonb", now)
        return "second_pass_done"
    # Final sweep. The drain barrier is re-checked here, not trusted from the second
    # pass: an older job can be retried (active again) or upload after that check. So:
    # wait while anything older runs, sweep, and complete only if nothing older became
    # active during the sweep (else the next tick sweeps again). Idempotent; repeated on
    # every tick that waits for Tracerfy.
    if _older_work_running(db, row) or not _sweep_r2(db, row, store, deadline):
        return "final_r2_waiting"
    if _older_work_running(db, row):
        return "final_r2_waiting"
    if db.execute(_OUT_AT_TRACERFY_SQL).first():
        db.rollback()
        return "tracerfy_waiting"
    _set(db, row.rule_id, "completed_at = :now", now)
    return "completed"


def drive_privacy_erasures(*, budget_s: float = _TICK_BUDGET_S, store: R2Store | None = None,
                           now: datetime | None = None) -> dict:
    """One bounded tick over every open erasure. Returns {rule_id: phase} plus counts."""
    from src.db.session import system_sync_session

    deadline = time.monotonic() + budget_s
    store = store or R2Store()
    with system_sync_session() as lock_db:
        if not lock_db.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": _LOCK_KEY}).scalar():
            return {"skipped": True}
        try:
            phases: dict[str, str] = {}
            with system_sync_session() as db:
                rows = db.execute(_OPEN_SQL).all()
                db.rollback()
                for row in rows:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        phases[row.rule_id] = _advance(db, row, store, deadline,
                                                       now or datetime.now(UTC))
                    except _OutOfTimeError:
                        db.rollback()
                        phases[row.rule_id] = "out_of_time"
                    except Exception as exc:  # noqa: BLE001 -- recorded, retried next tick
                        db.rollback()
                        db.execute(text(
                            "UPDATE privacy_erasures SET attempts = attempts + 1, "
                            "last_error = :e WHERE rule_id = CAST(:r AS uuid)"),
                            {"e": type(exc).__name__[:256], "r": row.rule_id})
                        db.commit()
                        phases[row.rule_id] = "error"
                        _logger.error("Privacy erasure %s failed: %s", row.rule_id[:8],
                                      type(exc).__name__)
            if phases:
                _logger.warning("Privacy erasure tick: %s", phases)
            return {"open": len(rows), "phases": phases}
        finally:
            lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock_db.commit()


try:  # pragma: no cover -- registration only
    from src.workers import app

    # Hard limits above the 240 s budget: a blocked query or R2 call cannot hold the
    # advisory lock for the worker default's hour. The soft limit raises inside the
    # tick (recorded as that rule's error); the lock's finally block releases it.
    @app.task(name="src.workers.privacy_erasure.drive_privacy_erasures",
              soft_time_limit=270, time_limit=300)
    def drive_privacy_erasures_task() -> dict:
        """Beat entry point: see drive_privacy_erasures."""
        return drive_privacy_erasures()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.error("Privacy erasure task NOT registered: %s", str(exc)[:120])
