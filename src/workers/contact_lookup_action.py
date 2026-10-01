"""The "look up contacts" action worker: `lookup_contacts(action_id)` (Phase 1b-2, 2b).

Contract: tasks/todo-lookup-contacts.md, "1b-2b" as amended by V1, V8/W7, W1 and the
"2b BUILD SPEC" with its consult rounds (Y1-Y4, Z1-Z3).

A customer confirmed a quote (2d), and the API committed an action in `dispatching`
with one `quoted` verdict row per lead it may buy. This task turns that into pending
skip-trace rows through the ONE claim path (`lock_job_for_claim` +
`claim_skip_trace_rows`, H3). It adds no spend path: the dispatcher, the caps, ingest
and metered billing do the rest, exactly as for a scrape. Settlement is derived later
from the pending rows by the reconciler (2c, S1); nothing here bills.

TWO TRANSACTIONS (W1), so a crash is always visible and never half-done:
  T1  `dispatching -> running` with a fresh lease token and a 10-minute expiry. It
      COMMITS alone, so a worker that dies after it leaves an expiring lease that the
      reconciler can see and take back.
  T2  locks the action, re-checks the lease (the fence), takes the job's claim lock,
      re-checks every gate, gives EVERY quoted lead exactly one verdict, claims the
      ones to buy, and moves `running -> claimed` with the lease cleared (15-15). ONE
      commit. A lost fence is a no-op.
Every fail / wait transition is its own committed transaction.

RECOVERY IS THE LEASE, NOT REDELIVERY (Y2). Celery redelivers a hard-killed task
(`acks_late`), but after T1 the action is `running`, so the redelivery no-ops by
design. The reconciler (2c) moves a `running` action whose lease expired back to
`dispatching` and re-publishes it. The task's time limits sit well under the lease,
so a task can never outlive its own lease.

THE STATE MACHINE IS CODE (V8, W7). Migration 101's guards restrict only user-scoped
sessions; the worker's system session passes them with no transition check at all.
So every status hop goes through `_move()` and every verdict through
`_set_verdicts()`, both checked against the matrices below, which the 2c reconciler
IMPORTS rather than copies.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger(__name__)

# How long T1's lease lasts. The task's own limits (below) are well under it.
LEASE_SECONDS = 600
# An action not started within this long of its confirm never starts: the reconciler
# expires it and nothing is bought (O-B). Enforced IN T1's CAS, not only by the
# reconciler, because a publish can be delivered late (2c consult AA1).
ACTION_DEADLINE_SECONDS = 30 * 60
_SOFT_TIME_LIMIT_S = 240
_HARD_TIME_LIMIT_S = 300

# ── The one transition matrix (V8 / W7) ───────────────────────────────────────

ACTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "dispatching": frozenset({"running", "expired"}),
    # -> dispatching: the action WAITS (kill switch, a busy claim lock) or the
    # reconciler took back an expired lease (O-B, Y2).
    "running": frozenset({"claimed", "failed", "dispatching"}),
    "claimed": frozenset({"settled"}),
    "settled": frozenset(),
    "failed": frozenset(),
    "expired": frozenset(),
}

_EXCLUDED = frozenset({
    "excluded_no_address", "excluded_placeholder_address",
    "excluded_settled_code_violation", "excluded_atip_policy", "excluded_not_traceable",
})
_TERMINAL_ANSWERS = frozenset({
    "answered_hit", "answered_miss", "unmatched_billable", "unmatched_unbilled",
    "errored_unsubmitted", "released",
})
VERDICT_TRANSITIONS: dict[str, frozenset[str]] = {
    # What this worker decides. An `excluded_*` only ever comes from a STRICTER
    # current policy than the quote's (15-14).
    "quoted": frozenset({
        "newly_queued", "reused", "already_answered", "in_progress_elsewhere",
        "ineligible", "abandoned", *_EXCLUDED,
    }),
    # What the reconciler (2c) derives from the pending row this action bought.
    "newly_queued": _TERMINAL_ANSWERS,
}

# Status column stamped on entering a status. The lease is SET only on entering
# `running`, and cleared on every other move, so a non-running action never
# carries a lease (15-15: a `claimed` action may wait for days by design).
_STAMPS = {
    "running": "started_at = COALESCE(started_at, now())",
    "claimed": "claimed_at = now()",
    "settled": "settled_at = now()",
}
# The count columns a move may write: the cache recomputable from the verdicts.
_COUNT_COLUMNS = frozenset({
    "claimed_count", "reused_count", "newly_queued_count", "billable_rows",
    "tracerfy_credits",
})
_VERDICT_CHUNK = 1000


class IllegalTransitionError(RuntimeError):
    """A move the matrix does not allow. A programming error, never a runtime state."""


def _event(db, action_id: str, user_id: str, frm: str | None, to: str, reason: str | None,
           *, lease_token: str | None = None, result_id: str | None = None) -> None:
    _events(db, [{
        "action_id": action_id, "user_id": user_id, "result_id": result_id,
        "frm": frm, "to": to, "reason": reason, "lease_token": lease_token,
    }])


def _events(db, rows: list[dict]) -> None:
    if not rows:
        return
    db.execute(
        text(
            "INSERT INTO contact_lookup_action_events "
            "(id, action_id, user_id, result_id, from_status, to_status, reason, lease_token) "
            "VALUES (CAST(:id AS uuid), CAST(:action_id AS uuid), CAST(:user_id AS uuid), "
            "        CAST(:result_id AS uuid), :frm, :to, :reason, :lease_token)"
        ),
        [{"id": str(uuid.uuid4()), **row} for row in rows],
    )


def _move(db, action_id: str, frm: str, to: str, reason: str | None = None, *,
          fence_token: str | None = None, new_token: str | None = None,
          counts: dict[str, int] | None = None) -> bool:
    """CAS the action `frm -> to` and write its event, in the caller's transaction.

    `fence_token`: the move happens only while that lease is still ours and unexpired.
    `new_token`: entering `running`, start a lease of LEASE_SECONDS under it.
    Returns False when the CAS matched nothing (another writer moved it first).
    """
    if to not in ACTION_TRANSITIONS.get(frm, frozenset()):
        raise IllegalTransitionError(f"action {frm!r} -> {to!r}")
    if (new_token is not None) != (to == "running"):
        raise IllegalTransitionError("a lease is started exactly when entering 'running'")
    sets = ["status = :to", "status_reason = :reason", "status_changed_at = now()"]
    params: dict[str, Any] = {"id": action_id, "frm": frm, "to": to, "reason": reason}
    if new_token is not None:
        sets += ["lease_token = :new_token",
                 "lease_expires_at = now() + make_interval(secs => :lease_s)"]
        params.update(new_token=new_token, lease_s=LEASE_SECONDS)
    else:
        sets += ["lease_token = NULL", "lease_expires_at = NULL"]
    if to in _STAMPS:
        sets.append(_STAMPS[to])
    for column, value in (counts or {}).items():
        if column not in _COUNT_COLUMNS:
            raise ValueError(f"not a count column: {column!r}")
        sets.append(f"{column} = :c_{column}")
        params[f"c_{column}"] = int(value)
    where = "id = CAST(:id AS uuid) AND status = :frm"
    if to == "running":
        where += " AND created_at > now() - make_interval(secs => :deadline_s)"
        params["deadline_s"] = ACTION_DEADLINE_SECONDS
    if fence_token is not None:
        where += " AND lease_token = :fence AND lease_expires_at > now()"
        params["fence"] = fence_token
    row = db.execute(
        text(f"UPDATE contact_lookup_actions SET {', '.join(sets)} "  # noqa: S608 - literals only
             f"WHERE {where} RETURNING user_id::text"),
        params,
    ).first()
    if row is None:
        return False
    _event(db, action_id, row[0], frm, to, reason, lease_token=new_token or fence_token)
    return True


# Statuses whose `status_reason` may change while the status stays put: a claimed
# action waiting on something only a human can resolve (2c consult AA3).
FLAGGABLE = frozenset({"claimed"})


def _flag(db, action_id: str, status: str, reason: str | None) -> bool:
    """Set (or, with None, clear) `status_reason` on an action that stays in `status`,
    with its event, in the caller's transaction. A flag is NOT a transition, so it is
    not in ACTION_TRANSITIONS; it is checked against FLAGGABLE instead.

    Returns True only when the value CHANGED, so a caller alerts once per change.
    """
    if status not in FLAGGABLE:
        raise IllegalTransitionError(f"no flag on {status!r}")
    row = db.execute(
        text("UPDATE contact_lookup_actions SET status_reason = :reason "
             "WHERE id = CAST(:id AS uuid) AND status = :status "
             "  AND status_reason IS DISTINCT FROM :reason "
             "RETURNING user_id::text"),
        {"id": action_id, "status": status, "reason": reason},
    ).first()
    if row is None:
        return False
    _event(db, action_id, row[0], status, status, reason or "flag_cleared")
    return True


def _set_verdicts(db, action_id: str, user_id: str, frm: str, verdicts: dict[str, str],
                  *, reasons: dict[str, str] | None = None) -> None:
    """Move each lead's verdict `frm -> verdicts[result_id]`, in the caller's transaction.

    Raises unless EXACTLY those rows moved: a verdict never silently misses a lead or
    rewrites one already decided (the `disposition = frm` guard). `reasons` adds a
    per-lead event for the leads the plan wants one for (held, abandoned).
    """
    allowed = VERDICT_TRANSITIONS.get(frm, frozenset())
    for to in set(verdicts.values()):
        if to not in allowed:
            raise IllegalTransitionError(f"verdict {frm!r} -> {to!r}")
    items = sorted(verdicts.items())
    moved = 0
    for start in range(0, len(items), _VERDICT_CHUNK):
        chunk = items[start:start + _VERDICT_CHUNK]
        params: dict[str, Any] = {"a": action_id, "u": user_id, "frm": frm}
        slots = []
        for i, (rid, to) in enumerate(chunk):
            params[f"r{i}"], params[f"d{i}"] = rid, to
            slots.append(f"(CAST(:r{i} AS uuid), CAST(:d{i} AS text))")
        moved += db.execute(
            text(
                "UPDATE contact_lookup_action_results c "  # noqa: S608 - bind slots only
                "SET disposition = v.d, decided_at = now() "
                f"FROM (VALUES {', '.join(slots)}) AS v(rid, d) "
                "WHERE c.action_id = CAST(:a AS uuid) AND c.user_id = CAST(:u AS uuid) "
                "  AND c.result_id = v.rid AND c.disposition = :frm"
            ),
            params,
        ).rowcount
    if moved != len(items):
        raise RuntimeError(
            f"action {action_id}: {moved} of {len(items)} verdict(s) moved from {frm!r}"
        )
    _events(db, [
        {"action_id": action_id, "user_id": user_id, "result_id": rid, "frm": frm,
         "to": verdicts[rid], "reason": reason, "lease_token": None}
        for rid, reason in sorted((reasons or {}).items())
    ])


def _quoted_ids(db, action_id: str, user_id: str) -> list[str]:
    return [str(r) for r in db.execute(
        text("SELECT result_id FROM contact_lookup_action_results "
             "WHERE action_id = CAST(:a AS uuid) AND user_id = CAST(:u AS uuid) "
             "  AND disposition = 'quoted' ORDER BY result_id"),
        {"a": action_id, "u": user_id},
    ).scalars()]


def _fail(db, action, token: str, reason: str) -> dict:
    """`running -> failed`, every still-quoted lead `abandoned` with its event. Commits."""
    aid, uid = str(action.id), str(action.user_id)
    if _move(db, aid, "running", "failed", reason, fence_token=token):
        ids = _quoted_ids(db, aid, uid)
        _set_verdicts(db, aid, uid, "quoted", dict.fromkeys(ids, "abandoned"),
                      reasons=dict.fromkeys(ids, reason))
    db.commit()
    _logger.warning("Contact lookup action %s failed: %s", aid, reason)
    return {"outcome": "failed", "reason": reason}


def _wait(db, action_id: str, token: str, reason: str) -> dict:
    """`running -> dispatching`, lease cleared: the reconciler re-drives it. Commits."""
    _move(db, action_id, "running", "dispatching", reason, fence_token=token)
    db.commit()
    _logger.info("Contact lookup action %s waiting: %s", action_id, reason)
    return {"outcome": "waiting", "reason": reason}


def _pinned_policy(snapshot, current: bool):
    """The quote's pinned ATIP policy AND the current one: only ever STRICTER (15-14).
    Parsed strictly (Y4): anything but a JSON `true` reads as off."""
    from src.api.contact_lookup_planner import PlannerPolicy

    policy = snapshot.get("policy") if isinstance(snapshot, dict) else None
    pinned = isinstance(policy, dict) and policy.get("pierce_cv_owner_skip_trace_enabled") is True
    return PlannerPolicy(pinned and current is True)


def _verdict_for(rec, enqueue_eligible, policy) -> tuple[str | None, dict | None]:
    """D1 steps 1-3 for one quoted lead: (its verdict, None), or (None, its payload)
    when it is still a candidate for the cache and the claim. First match wins."""
    from src.api import contact_lookup_planner as planner
    from src.scrapers.enrichment.skip_trace import build_pending_row_payload

    bucket = planner.classify(rec, policy).bucket
    if bucket == planner.IN_PROGRESS:
        return "in_progress_elsewhere", None
    if bucket == planner.ALREADY_ANSWERED:
        return "already_answered", None
    if bucket == planner.PREVIOUSLY_ATTEMPTED:
        # `errored` is ambiguous (a rejection, or a charged unmatched lookup): never
        # bought again here, exactly as the enqueue never re-reads it (15-3).
        return "ineligible", None
    if enqueue_eligible is not True:
        # Over quota, a superseded / same-run sibling, or no address left at all:
        # the enqueue's own SQL predicates (Y3, Z1).
        return "ineligible", None
    if bucket in planner.EXCLUSION_DISPOSITIONS:
        return planner.EXCLUSION_DISPOSITIONS[bucket], None
    payload = build_pending_row_payload(rec)
    if payload is None:  # classify said quotable, so the same call cannot say no
        return "excluded_not_traceable", None
    return None, payload


def _claim(db, action_id: str, token: str) -> dict:
    """T2. Everything from the fence to the one commit."""
    from sqlalchemy import and_, select

    from src.api.lead_actionability import actionable_condition
    from src.api.results_category import skip_trace_eligible_condition
    from src.config import settings
    from src.config.constants import SKIP_TRACE_ADDON_PLANS, normalize_plan
    from src.db.models import ContactLookupActionResult, Result
    from src.workers.skip_trace_capacity import credits_for
    from src.workers.skip_trace_claim import (
        ACCESS_ENDED,
        ACCESS_TRIAL,
        BLOCKED_ACCESS,
        claim_skip_trace_rows,
        lock_job_for_claim,
        paid_lookup_access,
        read_access_rows,
    )
    from src.workers.skip_trace_dispatcher import _job_delivered_sql
    from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE
    from src.workers.tasks_helpers.enrich import copy_cached_answer, settle_charged_unanswered

    action = db.execute(
        text("SELECT id::text, user_id::text, job_id::text, status, lease_token, "
             "       lease_expires_at > now() AS live, quoted_count, quote_snapshot "
             "FROM contact_lookup_actions WHERE id = CAST(:a AS uuid) FOR UPDATE"),
        {"a": action_id},
    ).first()
    if (action is None or action.status != "running" or action.lease_token != token
            or action.live is not True):
        db.rollback()
        return {"outcome": "fenced"}
    uid, jid = action.user_id, action.job_id

    # O-B: switched off -> wait, never fail. Checked before the job lock.
    if not settings.SKIP_TRACE_ENABLED or not settings.TRACERFY_API_TOKEN:
        return _wait(db, action_id, token, "kill_switch")

    # Lock order: action row -> job claim lock -> user row (the claim's). Everything
    # read from here on is read under the job lock (V1).
    lock_job_for_claim(db, jid)
    delivered = db.execute(
        text("SELECT 1 FROM jobs j WHERE j.id = CAST(:j AS uuid) "  # noqa: S608 - literal
             f"AND j.user_id = CAST(:u AS uuid) AND j.status = 'done' AND {_job_delivered_sql('j')}"),
        {"j": jid, "u": uid, "since": BILLING_STAMP_RELIABLE_SINCE},
    ).first()
    if delivered is None:
        return _fail(db, action, token, "job_not_delivered")

    account = read_access_rows(db, [uid], lock="").get(uid)
    if account is None or normalize_plan(account.plan) not in SKIP_TRACE_ADDON_PLANS:
        return _fail(db, action, token, "plan_not_eligible")
    access = paid_lookup_access(account)
    if access in BLOCKED_ACCESS:
        return _fail(db, action, token, f"access_{access}")

    # V1: the durable quoted set must be exactly the set the customer confirmed, and
    # ALL of it inside this tenant's job. A quoted row outside the job would otherwise
    # be read by nobody and stay `quoted` forever. Anything else fails closed.
    total, in_job = db.execute(
        text("SELECT count(*), count(r.id) FROM contact_lookup_action_results c "
             "LEFT JOIN results r ON r.id = c.result_id AND r.user_id = c.user_id "
             "                   AND r.job_id = CAST(:j AS uuid) "
             "WHERE c.action_id = CAST(:a AS uuid) AND c.user_id = CAST(:u AS uuid) "
             "  AND c.disposition = 'quoted'"),
        {"a": action_id, "u": uid, "j": jid},
    ).one()
    if not total == in_job == action.quoted_count:
        return _fail(db, action, token, "quoted_set_mismatch")

    # Z3: every quoted lead, each with the enqueue's SQL predicates as a COLUMN, so
    # a lead failing them still gets its verdict instead of being filtered away.
    rows = db.execute(
        select(
            Result,
            and_(
                Result.property_address.isnot(None),
                actionable_condition(),
                skip_trace_eligible_condition(),
            ).label("enqueue_eligible"),
        )
        .join(ContactLookupActionResult, and_(
            ContactLookupActionResult.result_id == Result.id,
            ContactLookupActionResult.user_id == Result.user_id,
            ContactLookupActionResult.action_id == action_id,
            ContactLookupActionResult.disposition == "quoted",
        ))
        .where(Result.user_id == uid, Result.job_id == jid)
        .order_by(Result.created_at, Result.id)
        .execution_options(populate_existing=True)
    ).all()

    policy = _pinned_policy(action.quote_snapshot,
                            bool(settings.PIERCE_CV_OWNER_SKIP_TRACE_ENABLED))
    verdicts: dict[str, str] = {}
    reasons: dict[str, str] = {}
    candidates: list[tuple[Any, dict]] = []
    for rec, enqueue_eligible in rows:
        verdict, payload = _verdict_for(rec, enqueue_eligible, policy)
        if verdict is not None:
            verdicts[str(rec.id)] = verdict
        else:
            candidates.append((rec, payload))

    # D1 step 4: charged but unanswered -> settled exactly as the enqueue settles it.
    kept, _ = settle_charged_unanswered(db, uid, [rec for rec, _p in candidates])
    kept_ids = {str(rec.id) for rec in kept}
    to_claim: list[dict] = []
    for rec, payload in candidates:
        rid = str(rec.id)
        if rid not in kept_ids:
            verdicts[rid] = "already_answered"
        elif copy_cached_answer(db, uid, rec, payload):  # D1 step 5
            verdicts[rid] = "reused"
        else:
            to_claim.append(payload)

    # D1 step 6: the claim, in the quote's window order (trial room walks it).
    report: dict = {}
    won = set(claim_skip_trace_rows(db, to_claim, report=report, action_id=action_id))
    held = set(report.get("held_ids", []))
    held_reason = ("trial_allowance" if report.get("access") == ACCESS_TRIAL
                   else f"access_{report.get('access', ACCESS_ENDED)}")
    credits = 0
    lost: list[str] = []
    for payload in to_claim:
        rid = str(payload["result_id"])
        if rid in won:
            verdicts[rid] = "newly_queued"
            credits += credits_for(payload["trace_type"])
        elif rid in held:
            verdicts[rid] = "ineligible"
            reasons[rid] = held_reason
        else:
            lost.append(rid)
    if lost:
        # Refused or lost a race: the verdict follows what the lead is NOW.
        for rid, status in db.execute(
            text("SELECT id::text, skip_trace_status FROM results "
                 "WHERE id = ANY(CAST(:ids AS uuid[])) AND user_id = CAST(:u AS uuid)"),
            {"ids": lost, "u": uid},
        ).all():
            verdicts[rid] = ("in_progress_elsewhere" if status in ("queued", "submitted")
                             else "already_answered" if status in ("hit", "miss")
                             else "ineligible")

    quoted = {str(rec.id) for rec, _e in rows}
    if set(verdicts) != quoted:
        raise RuntimeError(f"action {action_id}: verdicts do not cover the quoted set")
    _set_verdicts(db, action_id, uid, "quoted", verdicts, reasons=reasons)

    newly = sum(1 for v in verdicts.values() if v == "newly_queued")
    reused = sum(1 for v in verdicts.values() if v == "reused")
    counts = {"newly_queued_count": newly, "reused_count": reused,
              "claimed_count": newly + reused, "tracerfy_credits": credits}
    if not _move(db, action_id, "running", "claimed", None, fence_token=token, counts=counts):
        raise RuntimeError(f"action {action_id}: lease lost under the row lock")
    db.commit()
    _logger.info(
        "Contact lookup action %s claimed: quoted=%d newly_queued=%d reused=%d credits=%d",
        action_id, len(quoted), newly, reused, credits,
    )
    return {"outcome": "claimed", **counts, "quoted": len(quoted)}


def _failure_reason(exc: BaseException) -> str:
    from src.workers.skip_trace_claim import ClaimUnenforcedError

    if isinstance(exc, ClaimUnenforcedError):
        return "claim_unenforced"
    # 55P03 lock_not_available: lock_job_for_claim's bounded wait ran out.
    if getattr(getattr(exc, "orig", None), "pgcode", None) == "55P03":
        return "claim_lock_busy"
    return "worker_error"


def run_action(action_id: str) -> dict:
    """The task body, callable directly (the task is a thin wrapper)."""
    from src.db.session import system_sync_session

    token = uuid.uuid4().hex
    with system_sync_session() as db:  # T1
        started = _move(db, action_id, "dispatching", "running", None, new_token=token)
        db.commit()
    if not started:
        # Another delivery holds it, or it is past dispatch: nothing to do.
        return {"outcome": "not_dispatching"}

    with system_sync_session() as db:  # T2
        try:
            return _claim(db, action_id, token)
        except Exception as exc:  # noqa: BLE001 - every failure waits, unknown ones re-raise
            db.rollback()
            reason = _failure_reason(exc)
            _wait(db, action_id, token, reason)
            if reason == "claim_unenforced":
                from src.workers.ops_alerts import send_ops_alert

                try:
                    send_ops_alert(
                        "skip_trace_claim_unenforced", "contact_lookup_action",
                        "Skip-trace claims are refused: migration 100 is not in place",
                        f"Contact lookup action {action_id} is waiting: the unique index "
                        "that stops a lead being looked up twice is missing, invalid or "
                        f"not the expected index. Nothing was charged. {exc}",
                    )
                except Exception:  # noqa: BLE001 - an alert failure must not mask the wait
                    _logger.exception("unenforced-claim alert failed to send")
            if reason == "worker_error":
                _logger.exception("Contact lookup action %s: T2 failed", action_id)
                raise
            return {"outcome": "waiting", "reason": reason}


@app.task(
    name="src.workers.contact_lookup_action.lookup_contacts",
    soft_time_limit=_SOFT_TIME_LIMIT_S,
    time_limit=_HARD_TIME_LIMIT_S,
)
def lookup_contacts(action_id: str) -> dict:
    """Claim a confirmed action's quoted leads into the paid queue. Idempotent."""
    try:
        aid = str(uuid.UUID(str(action_id)))
    except ValueError:
        _logger.warning("lookup_contacts: not an action id: %r", str(action_id)[:64])
        return {"outcome": "invalid_id"}
    return run_action(aid)
