"""The privacy enforcer sweep (beat): stored state converges on the rules.

Design section 6 ("an enforcer sweep (beat) re-applies rules so stored state
converges"); plan tasks/todo-privacy-phase6-p5.md P5c. Writers that add a subject
check their own rows inline (src/privacy/enforce.py); this sweep is the backstop for
everything else: every row stored before a rule activated, and any row a writer did
not reach.

One tick: load ONE rule snapshot. Generation 0 means no rule has ever been active, so
every row's stamp is current: return after that single query. Otherwise walk the
unflagged rows whose stamp is behind the generation in (privacy_checked_gen, id) order
(migration 130's index), in batches, each authorized, flagged and stamped by
enforce_rows and committed on its own, until the time budget runs out. A rule that
activates mid-tick raises the generation, so the rows stamped with the older one are
read again next tick.

No lock: every write is idempotent (a flag is set only on an unflagged row, a stamp
only rises), and the budget keeps a tick well inside the schedule's period.
RuleSnapshotError propagates: the tick fails loudly and the next one retries; nothing
is ever stamped unchecked.
"""
from __future__ import annotations

import time

from sqlalchemy import text

from src.db.session import system_sync_session
from src.privacy.authorize import load_rule_snapshot
from src.privacy.enforce import EnforceStats, enforce_rows
from src.utils.logger import setup_logger

_logger = setup_logger("worker.privacy_enforcer")

_BATCH = 500
_TICK_BUDGET_S = 240.0  # the beat period is 10 minutes

_NEXT_BATCH_SQL = text(
    "SELECT id, privacy_checked_gen FROM results "
    "WHERE privacy_suppressed_at IS NULL AND privacy_checked_gen < :gen "
    "  AND (privacy_checked_gen, id) > (:after_gen, CAST(:after_id AS uuid)) "
    "ORDER BY privacy_checked_gen, id LIMIT :n"
)
_START = (-1, "00000000-0000-0000-0000-000000000000")


def enforce_privacy_rules(*, budget_s: float = _TICK_BUDGET_S, batch: int = _BATCH) -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    deadline = time.monotonic() + budget_s
    totals = EnforceStats()
    batches = 0
    with system_sync_session() as db:
        snapshot = load_rule_snapshot(db)
        gen = snapshot.generation
        if gen:
            after_gen, after_id = _START
            while time.monotonic() < deadline:
                page = db.execute(_NEXT_BATCH_SQL, {
                    "gen": gen, "after_gen": after_gen, "after_id": after_id, "n": batch,
                }).all()
                if not page:
                    break
                totals.add(enforce_rows(db, snapshot, [r.id for r in page]))
                db.commit()
                batches += 1
                after_gen, after_id = page[-1].privacy_checked_gen, str(page[-1].id)
    stats = {"generation": gen, "batches": batches, "checked": totals.checked,
             "flagged": totals.flagged, "unscoped": totals.unscoped}
    if totals.flagged or totals.unscoped:
        # Counts only: never a name, an address or a rule id.
        _logger.warning("Privacy enforcer: %s", stats)
    return stats


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.privacy_enforcer.enforce_privacy_rules")
    def enforce_privacy_rules_task() -> dict:
        """Beat entry point: see enforce_privacy_rules."""
        return enforce_privacy_rules()
except Exception as exc:  # pragma: no cover -- import-time safety only
    # Loud: beat would keep publishing a task nobody runs.
    _logger.error("Privacy enforcer task NOT registered: %s", str(exc)[:120])
