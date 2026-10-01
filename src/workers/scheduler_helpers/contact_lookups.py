"""Body of the reconcile_contact_lookups beat task (Phase 1b-2, step 2c).

Contract: tasks/todo-lookup-contacts.md, "## Phase 1b-2c — the reconciler" as amended
by AA1-AA9, AB1-AB4 and the 2c-ii diff review (AC1-AC3).

Four passes over the contact-lookup actions, each action in its OWN transaction,
each action row locked `FOR UPDATE SKIP LOCKED` (a running worker or another tick is
never waited on), every move through the ONE state machine in
`src.workers.contact_lookup_action` (W7):

  P1 expire      `dispatching` past the 30-min deadline -> `expired`, every quoted
                 lead `abandoned` with its event. Nothing was bought (O-B).
  P2 take back   `running` whose lease expired -> `dispatching` (Y2). The claim is
                 one transaction, so nothing is half-done.
  P3 re-publish  `dispatching` not yet started -> `lookup_contacts` again (15-2). The
                 publish ATTEMPT is stamped, so every waiting action rotates (AA7).
  P4 settle      `claimed`: each `newly_queued` lead's verdict is DERIVED from the
                 pending row this action bought (S1), then counts, then `settled`
                 once nothing is left (V4). A lead whose outcome nobody can know yet
                 flags the action for a human instead (AA4, AA8), once (AA3).

IT NEVER BILLS. Billing happens per pending row at ingest. The only billing call here
is `queue_accepted_all`, a read, so an action's `unmatched` lead is called billable
exactly when billing's rule says so (V2, W3). Until O-C persists the decision billing
made, this recomputes the same rule (AA2); 2d is gated on O-C, so no customer sees it.

FAIRNESS (AA5, AB2, AC1-AC3). Every selection is per action (EXISTS, never a join one
action can fill), bounded, skips rows another transaction holds BEFORE its LIMIT (so a
locked prefix cannot starve the rest), reads each lead's pending row exactly as the
visit does (`_CURRENT_ROW`), and consumes the work it selected. One action failing is
logged and counted, and the pass goes on to the next (AC2).
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

_EXPIRE_LIMIT = 100
_LEASE_LIMIT = 100
_PUBLISH_LIMIT = 100
_MAPPABLE_LIMIT = 50
_SETTLE_LIMIT = 20
_STUCK_LIMIT = 20
_UNFLAG_LIMIT = 20

# P3: a confirm that never published is re-published after a minute; a published
# action still waiting is re-published every five.
_FIRST_PUBLISH_AFTER_S = 60
_REPUBLISH_EVERY_S = 300

# Why a claimed action waits for a human (`status_reason`), in reporting priority.
FLAG_PENDING_ROW_MISSING = "pending_row_missing"
FLAG_RESULT_STATE_UNEXPECTED = "result_state_unexpected"
FLAG_PROVIDER_RECONCILIATION = "provider_reconciliation_required"
FLAGS = (FLAG_PENDING_ROW_MISSING, FLAG_RESULT_STATE_UNEXPECTED, FLAG_PROVIDER_RECONCILIATION)

_BILLABLE_VERDICTS = ("answered_hit", "answered_miss", "unmatched_billable")

# THE pending row of a `newly_queued` lead `c`: the one this action bought, pinned to
# the action, the lead AND the tenant. A lead has at most one in practice (the action
# claims once); should it ever carry two, the ACTIVE one decides, so a lead with a
# lookup in flight is never settled. Every selection predicate AND the visit read
# this same row (AC3), so what selects an action is exactly what the visit acts on.
_CURRENT_ROW_SQL = (
    "(SELECT p0.* FROM pending_skip_trace_rows p0 "
    "  WHERE p0.action_id = c.action_id AND p0.result_id = c.result_id "
    "    AND p0.user_id = c.user_id "
    "  ORDER BY (p0.status IN ('queued', 'submitting', 'submitted')) DESC, "
    "           p0.enqueued_at DESC, p0.id DESC LIMIT 1)"
)
_CURRENT_ROW = f" LEFT JOIN LATERAL {_CURRENT_ROW_SQL} p ON true "
# `c` = a newly_queued lead of action `a`, with its current row `p` (or NULL).
_LEADS_OF_A = (
    "SELECT 1 FROM contact_lookup_action_results c" + _CURRENT_ROW +  # noqa: S608 - literals
    " LEFT JOIN results r ON r.id = c.result_id AND r.user_id = c.user_id "
    " LEFT JOIN skip_trace_queues q ON q.tracerfy_queue_id = p.tracerfy_queue_id "
    " WHERE c.action_id = a.id AND c.user_id = a.user_id "
    "   AND c.disposition = 'newly_queued' "
)

# The four blockers (AB2): each an EXISTS over action `a`, mirroring the visit.
_STALE_SUBMITTING = (f"EXISTS ({_LEADS_OF_A} AND p.status = 'submitting' "
                     "AND p.submitted_at < now() - make_interval(secs => :stale_s))")
_SUBMITTED_IN_ERRORED_QUEUE = (f"EXISTS ({_LEADS_OF_A} AND p.status = 'submitted' "
                               "AND q.status = 'errored')")
_ROW_MISSING = f"EXISTS ({_LEADS_OF_A} AND p.id IS NULL)"
_RESULT_UNEXPECTED = (f"EXISTS ({_LEADS_OF_A} AND p.status = 'completed' "
                      "AND r.skip_trace_status IS DISTINCT FROM 'hit' "
                      "AND r.skip_trace_status IS DISTINCT FROM 'miss')")
_ANY_BLOCKER = (f"({_STALE_SUBMITTING} OR {_SUBMITTED_IN_ERRORED_QUEUE} "
                f"OR {_ROW_MISSING} OR {_RESULT_UNEXPECTED})")
# A lead the visit can map right now.
_MAPPABLE = (f"EXISTS ({_LEADS_OF_A} AND (p.status IN ('unmatched', 'errored', 'cancelled') "
             "OR (p.status = 'completed' AND r.skip_trace_status IN ('hit', 'miss'))))")
_NOTHING_ACTIVE = (f"NOT EXISTS ({_LEADS_OF_A} "
                   "AND p.status IN ('queued', 'submitting', 'submitted'))")


def _stale_s() -> int:
    from src.workers.skip_trace_dispatcher import _STALE_CLAIM_AFTER

    return int(_STALE_CLAIM_AFTER.total_seconds())


def _candidates(db, predicate: str, order: str, limit: int, params: dict) -> list[str]:
    """Ids of actions matching `predicate`, SKIPPING rows another transaction holds
    before the LIMIT applies, so a locked prefix cannot starve the rest (AC1). The
    locks are released at once; each action is re-locked and re-checked on its own."""
    ids = [str(r) for r in db.execute(
        text(f"SELECT a.id FROM contact_lookup_actions a WHERE {predicate} "  # noqa: S608 - literals
             f"ORDER BY {order} LIMIT {int(limit)} FOR UPDATE OF a SKIP LOCKED"),
        params,
    ).scalars()]
    db.rollback()
    return ids


def _locked(db, action_id: str, predicate: str, params: dict):
    """The action, row-locked, only if it STILL matches `predicate`; else None (and
    nothing is held). SKIP LOCKED: whoever holds it is acting on it right now."""
    row = db.execute(
        text("SELECT a.id::text AS id, a.user_id::text AS user_id, a.status_reason "  # noqa: S608
             f"FROM contact_lookup_actions a WHERE a.id = CAST(:a AS uuid) AND {predicate} "
             "FOR UPDATE SKIP LOCKED"),
        {**params, "a": action_id},
    ).first()
    if row is None:
        db.rollback()
    return row


def _each(db, summary: dict, pass_name: str, ids: list[str], predicate: str, params: dict,
          act: Callable) -> None:
    """Visit each action in its own transaction (AC2): lock it, re-check `predicate`,
    `act` (which commits and may return a callable to run AFTER the commit). A failure
    is rolled back, logged with its id and counted, and the next action still runs."""
    for aid in ids:
        try:
            row = _locked(db, aid, predicate, params)
            if row is None:
                continue
            after = act(db, aid, row)
        except Exception:  # noqa: BLE001 - one bad action must not stall the pass
            db.rollback()
            summary["errors"] += 1
            _logger.exception("Contact lookup reconcile %s: action %s failed", pass_name, aid)
            continue
        if after is not None:
            after()


# ── P1 expire ────────────────────────────────────────────────────────────────


def _expire(db, summary: dict) -> None:
    from src.workers.contact_lookup_action import (
        ACTION_DEADLINE_SECONDS,
        _move,
        _quoted_ids,
        _set_verdicts,
    )

    predicate = ("a.status = 'dispatching' "
                 "AND a.created_at <= now() - make_interval(secs => :d)")
    params = {"d": ACTION_DEADLINE_SECONDS}

    def act(db_, aid, row):
        _move(db_, aid, "dispatching", "expired", "deadline")
        ids = _quoted_ids(db_, aid, row.user_id)
        _set_verdicts(db_, aid, row.user_id, "quoted", dict.fromkeys(ids, "abandoned"),
                      reasons=dict.fromkeys(ids, "deadline"))
        db_.commit()
        summary["expired"] += 1
        _logger.info("Contact lookup action %s expired: %d lead(s) abandoned", aid, len(ids))

    ids = _candidates(db, predicate, "a.created_at, a.id", _EXPIRE_LIMIT, params)
    _each(db, summary, "expire", ids, predicate, params, act)


# ── P2 lease take-back ───────────────────────────────────────────────────────


def _take_back(db, summary: dict) -> None:
    from src.workers.contact_lookup_action import _move

    predicate = "a.status = 'running' AND a.lease_expires_at < now()"

    def act(db_, aid, _row):
        _move(db_, aid, "running", "dispatching", "lease_expired")
        db_.commit()
        summary["lease_taken_back"] += 1
        _logger.warning("Contact lookup action %s: lease expired, handed back", aid)

    ids = _candidates(db, predicate, "a.lease_expires_at, a.id", _LEASE_LIMIT, {})
    _each(db, summary, "take_back", ids, predicate, {}, act)


# ── P3 re-publish ────────────────────────────────────────────────────────────


def _republish(db, summary: dict) -> None:
    from src.config import settings
    from src.workers.contact_lookup_action import ACTION_DEADLINE_SECONDS, lookup_contacts

    if not settings.SKIP_TRACE_ENABLED or not settings.TRACERFY_API_TOKEN:
        # The worker would only hand it straight back (O-B). It waits, and P1
        # expires it at the deadline if the switch stays off.
        return
    predicate = (
        "a.status = 'dispatching' "
        "AND a.created_at > now() - make_interval(secs => :d) "
        "AND ((a.dispatched_at IS NULL AND a.created_at < now() - make_interval(secs => :first)) "
        "     OR a.dispatched_at < now() - make_interval(secs => :every))"
    )
    params = {"d": ACTION_DEADLINE_SECONDS, "first": _FIRST_PUBLISH_AFTER_S,
              "every": _REPUBLISH_EVERY_S}

    def act(db_, aid, _row):
        try:
            lookup_contacts.apply_async(args=[aid])
            summary["republished"] += 1
        except Exception:  # noqa: BLE001 - the next tick retries; the stamp rotates it
            _logger.exception("Contact lookup action %s: re-publish failed", aid)
            summary["publish_failed"] += 1
        # Stamped on every ATTEMPT (AA7): a dead broker cannot pin the oldest at the
        # head. For the system role this column means "last publish attempt".
        db_.execute(text("UPDATE contact_lookup_actions SET dispatched_at = now() "
                         "WHERE id = CAST(:a AS uuid)"), {"a": aid})
        db_.commit()

    ids = _candidates(db, predicate, "COALESCE(a.dispatched_at, a.created_at), a.id",
                      _PUBLISH_LIMIT, params)
    _each(db, summary, "republish", ids, predicate, params, act)


# ── P4 settle claimed ────────────────────────────────────────────────────────


def _p4_candidates(db) -> list[str]:
    """Claimed actions with WORK, in four bounded groups (AA5, AB2). Each visit
    consumes what selected it, so none can hold a slot while it waits:
      (a) a lead mappable now;
      (b) unflagged, nothing active: settle it, or discover its blocker;
      (c) unflagged with ANY blocker (stuck, missing row, unexpected result), even
          while another lead is still in flight: flag it now, not when the rest moves;
      (d) flagged, and its blocker is gone: clear it.
    A permanently blocked action (a missing row, an unexpected result) is in none of
    them once flagged, so it neither re-alerts nor starves anyone."""
    params = {"stale_s": _stale_s(), "flags": list(FLAGS)}
    claimed = "a.status = 'claimed'"
    groups = (
        (f"{claimed} AND {_MAPPABLE}", _MAPPABLE_LIMIT),
        (f"{claimed} AND a.status_reason IS NULL AND {_NOTHING_ACTIVE}", _SETTLE_LIMIT),
        (f"{claimed} AND a.status_reason IS NULL AND {_ANY_BLOCKER}", _STUCK_LIMIT),
        (f"{claimed} AND a.status_reason = ANY(CAST(:flags AS text[])) "
         f"AND NOT {_ANY_BLOCKER}", _UNFLAG_LIMIT),
    )
    seen: dict[str, None] = {}
    for predicate, limit in groups:
        for aid in _candidates(db, predicate, "a.claimed_at, a.id", limit, params):
            seen.setdefault(aid)
    return list(seen)


def _settle(db, aid: str, row, summary: dict) -> Callable | None:
    """One claimed action, one transaction (V4). Returns the post-commit alert, if a
    flag was newly raised."""
    from src.api.billing.skip_trace_usage import queue_accepted_all
    from src.workers.contact_lookup_action import _flag, _move, _set_verdicts

    uid = row.user_id
    leads = db.execute(
        text("SELECT c.result_id::text AS rid, p.id AS pending_id, p.status, "  # noqa: S608
             "       p.tracerfy_queue_id AS qid, "
             "       p.submitted_at < now() - make_interval(secs => :stale_s) AS stale, "
             "       q.status AS queue_status, r.skip_trace_status AS result_status "
             "FROM contact_lookup_action_results c" + _CURRENT_ROW +
             "LEFT JOIN skip_trace_queues q ON q.tracerfy_queue_id = p.tracerfy_queue_id "
             "LEFT JOIN results r ON r.id = c.result_id AND r.user_id = c.user_id "
             "WHERE c.action_id = CAST(:a AS uuid) AND c.user_id = CAST(:u AS uuid) "
             "  AND c.disposition = 'newly_queued'"),
        {"a": aid, "u": uid, "stale_s": _stale_s()},
    ).all()

    verdicts: dict[str, str] = {}
    blockers: set[str] = set()
    accepted: dict[int, bool] = {}  # once per distinct queue (AA6)
    for lead in leads:
        status = lead.status
        if lead.pending_id is None:
            blockers.add(FLAG_PENDING_ROW_MISSING)
        elif status == "completed":
            if lead.result_status == "hit":
                verdicts[lead.rid] = "answered_hit"
            elif lead.result_status == "miss":
                verdicts[lead.rid] = "answered_miss"
            else:  # purged, anything else: never guessed (AA4)
                blockers.add(FLAG_RESULT_STATE_UNEXPECTED)
        elif status == "unmatched":
            if lead.qid not in accepted:
                accepted[lead.qid] = (lead.qid is not None
                                      and queue_accepted_all(db, lead.qid))
            verdicts[lead.rid] = ("unmatched_billable" if accepted[lead.qid]
                                  else "unmatched_unbilled")
        elif status == "errored":
            verdicts[lead.rid] = "errored_unsubmitted"
        elif status == "cancelled":
            verdicts[lead.rid] = "released"
        elif ((status == "submitting" and lead.stale)
              or (status == "submitted" and lead.queue_status == "errored")):
            blockers.add(FLAG_PROVIDER_RECONCILIATION)
        # else: active and moving. Nothing to decide yet.
    if verdicts:
        _set_verdicts(db, aid, uid, "newly_queued", verdicts)
        summary["mapped"] += len(verdicts)

    billable = db.execute(
        text("SELECT count(*) FROM contact_lookup_action_results "
             "WHERE action_id = CAST(:a AS uuid) AND user_id = CAST(:u AS uuid) "
             "  AND disposition = ANY(CAST(:b AS text[]))"),
        {"a": aid, "u": uid, "b": list(_BILLABLE_VERDICTS)},
    ).scalar_one()
    raised = None
    if len(verdicts) == len(leads):  # nothing newly_queued is left
        _move(db, aid, "claimed", "settled", None, counts={"billable_rows": billable})
        summary["settled"] += 1
    else:
        want = next((f for f in FLAGS if f in blockers), None)
        if _flag(db, aid, "claimed", want):
            summary["flagged" if want else "unflagged"] += 1
            raised = want
        db.execute(text("UPDATE contact_lookup_actions SET billable_rows = :b "
                        "WHERE id = CAST(:a AS uuid)"), {"a": aid, "b": billable})
    db.commit()
    if raised:
        return lambda: _alert(aid, raised)  # after the commit (AB3)
    return None


def _alert(aid: str, flag: str) -> None:
    """Once per change of flag."""
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "contact_lookup", f"{flag}_{aid}",
            f"Contact lookup action needs a human: {flag}",
            f"Contact lookup action {aid} cannot settle on its own ({flag}). "
            "pending_row_missing: a lead it bought has no queue row. "
            "result_state_unexpected: a completed lookup's lead is neither hit nor miss. "
            "provider_reconciliation_required: a lookup is stuck mid-submission or its "
            "Tracerfy batch errored. Nothing is billed by this reconciler; the action "
            "stays 'claimed' until the cause is resolved.",
        )
    except Exception:  # noqa: BLE001 - alerting is best-effort; the flag is durable
        _logger.exception("contact-lookup flag alert failed for %s", aid)


def _settle_claimed(db, summary: dict) -> None:
    _each(db, summary, "settle", _p4_candidates(db), "a.status = 'claimed'", {},
          lambda db_, aid, row: _settle(db_, aid, row, summary))


def _reconcile_contact_lookups_impl() -> dict:
    from src.db.session import system_sync_session

    summary = dict.fromkeys(
        ("expired", "lease_taken_back", "republished", "publish_failed", "mapped",
         "settled", "flagged", "unflagged", "errors"), 0)
    with system_sync_session() as db:
        for step in (_expire, _take_back, _republish, _settle_claimed):
            try:
                step(db, summary)
            except Exception:  # noqa: BLE001 - each pass is independent
                db.rollback()
                summary["errors"] += 1
                _logger.exception("Contact lookup reconcile: %s failed", step.__name__)
    if any(summary.values()):
        _logger.info("Contact lookup reconcile: %s", summary)
    return summary
