"""Deliver a held code violation once it is complete, and bill it then (owner, 2026-10-30).

WHY THIS EXISTS
---------------
The qualification gate (src/api/lead_qualification.py, #542) holds a code violation back
when its run cannot name the owner, the parcel or the mailing address. The background
owner and mailing sweeps keep working on held rows (cv_owner_recovery.py and
pierce_cv_owner_recovery.py, late-delivery Phase A), so a held row can become complete
hours later. Before this module it then waited for the account's NEXT run to take its
claim; now it is delivered as soon as it passes the policy.

WHAT A LATE DELIVERY IS
-----------------------
One transaction per row, under the locks of everything it reads, in the order
jobs -> results -> delivered_records -> users (reservation and billing take
jobs -> users, claim transfer takes results -> delivered_records, so no cycle):
  1. the run is still done and belongs to the row's account;
  2. the row is still held, not a duplicate, not superseded, never late-delivered,
     and qualifies NOW (qualified_sql: verified owner, parcel, both addresses, case);
  3. it still holds its claim (delivered_records.first_result_id = the row): a later
     run that took the claim has already delivered and billed the property;
  4. the account is granted ONE record by the same rule a run's reservation uses
     (tasks.grant_records: charge eligibility, window rollover, pending downgrade,
     all under the users row lock, unlimited plans charged too);
  5. the hold mark is removed and `enrichment_data.late_delivery` is written: the
     per-row proof it was charged, so it can never be charged twice.
No grant (no quota left, account frozen or ended): nothing is written and the row
stays held; it is tried again next tick, and a later run can still take its claim.

WHAT IT NEVER CHANGES
---------------------
The run's own numbers and artifacts: jobs.billed_count, record_count, the run
breakdown snapshot (breakdown_from_job requires new == record_count == billed_count),
the R2 file and the e-mail already sent. The row appears in its run's Results list and
in the in-app download (both read rows live); files already downloaded do not change.
Same-run siblings stay held (they are duplicates and never bill). No dialer push and no
paid skip trace for a late row yet (late delivery Phase D, an owner decision); the user
can look up its contacts by hand. No PII is logged: ids and counts only.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy import text as sa_text

from src.api.lead_actionability import (
    DELIVERY_EXCLUDED_KEY,
    NOT_QUALIFIED,
    not_privacy_suppressed_sql,
)
from src.api.lead_qualification import POLICY_CODE_VIOLATION, POLICY_KEY, qualified_sql
from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.cv_late_delivery")

LATE_KEY = "late_delivery"
# late_delivery.contacts: "pending" (the run's scraper has skip tracing on; not asked yet),
# "requested" (the enqueue was called for this row: its own gates, claim and dispatcher
# decided the rest), "not_requested" (a gate that will not change: skip tracing off for the
# scraper now, a Starter plan, not the owner). Never asked twice.
CONTACTS_KEY = "contacts"
CONTACTS_PENDING = "pending"
# late_delivery.dialer: "pending" (the run's scraper pushes to a dialer; not decided yet), then
# exactly one terminal state: "pushed"; "via_sweep" (the run's sweep sends or sent it); for a
# generic webhook only, "skipped_overlap" (pushed too close to the run's own push to know
# whether that push carried it) and "send_failed" (the request could not be queued; never
# re-sent, a webhook has no per-lead idempotency); "not_ready" (no dialable contact when
# decided); "not_eligible" (no Business+ plan or no destination now); "privacy_withheld"
# (a privacy suppression rule withholds or holds the row for review; never sent).
DIALER_KEY = "dialer"
DIALER_PENDING = "pending"


def uses_outbox(deliver) -> bool:
    """The run's dialer is a native vendor that pushes from the idempotent outbox."""
    from src.workers.dialer_connectors import get_connector

    return isinstance(deliver, dict) and get_connector(deliver.get("dialer_type")).uses_outbox


def has_dialer_destination(deliver) -> bool:
    """The sweep's rule (scheduler_helpers/dialer.py): a generic webhook URL, or a native
    vendor dialer_type that pushes from the outbox."""
    if not isinstance(deliver, dict):
        return False
    url = deliver.get("dialer_webhook_url")
    kind = deliver.get("dialer_type")
    return bool(url) or (bool(kind) and kind != "generic_webhook")
# Rows per tick. Each is one short transaction; the bound only keeps a backlog from
# holding the beat worker.
_BATCH_ROWS = 200
# Rows per account per tick, taken round-robin (every account's oldest row before any
# account's second), so one account's backlog never crowds the others out (Codex P2).
_PER_ACCOUNT_ROWS = 20
_STATEMENT_TIMEOUT_MS = 10000

_HELD = (f"r.enrichment_data::jsonb->>'{POLICY_KEY}' = '{POLICY_CODE_VIOLATION}' "
         f"AND r.enrichment_data::jsonb->>'{DELIVERY_EXCLUDED_KEY}' = '{NOT_QUALIFIED}' "
         f"AND NOT (r.enrichment_data::jsonb ? '{LATE_KEY}')")
# Not withheld by a privacy rule (migration 126): a held row a person asked to be removed
# from must never be delivered or billed late. In discovery AND under the row lock, so a
# row flagged between the two is skipped as stale.
_ELIGIBLE = (f"r.is_duplicate = false AND r.duplicate_reason IS NULL "
             f"AND jsonb_typeof(r.enrichment_data::jsonb) = 'object' AND {_HELD} "
             f"AND {qualified_sql('r')} AND {not_privacy_suppressed_sql('r')}")

# Discovery only (ids, on the system session: it spans accounts). Round-robin across
# accounts, oldest first within each. Every condition is decided again under the locks,
# in the account's own RLS session, so a row that changes after this read is skipped.
_CANDIDATES_SQL = f"""
    SELECT id, user_id, job_id FROM (
      SELECT r.id::text AS id, r.user_id::text AS user_id, r.job_id::text AS job_id,
             r.created_at,
             row_number() OVER (PARTITION BY r.user_id ORDER BY r.created_at, r.id) AS rn
      FROM results r
      JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id AND j.status = 'done'
      JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
                             AND sc.record_type = 'code_violation'
      JOIN delivered_records dr ON dr.user_id = r.user_id AND dr.dedup_hash = r.dedup_hash
                               AND dr.first_result_id = r.id
      WHERE {_ELIGIBLE}
    ) c
    WHERE rn <= :per_account
    ORDER BY rn, created_at, id
    LIMIT :batch
"""  # noqa: S608 -- splices only module constants; every value is bound

_LOCK_JOB_SQL = ("SELECT sc.skip_trace_enabled, sc.deliver FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
                 "WHERE j.id = CAST(:jid AS uuid) AND j.user_id = CAST(:uid AS uuid) "
                 "AND sc.user_id = j.user_id AND sc.record_type = 'code_violation' "
                 "AND j.status = 'done' FOR UPDATE OF j")
_LOCK_ROW_SQL = f"""
    SELECT r.dedup_hash FROM results r
    WHERE r.id = CAST(:rid AS uuid) AND r.user_id = CAST(:uid AS uuid)
      AND r.job_id = CAST(:jid AS uuid) AND {_ELIGIBLE}
    FOR UPDATE OF r
"""  # noqa: S608 -- splices only module constants; every value is bound
_LOCK_CLAIM_SQL = ("SELECT first_result_id::text FROM delivered_records "
                   "WHERE user_id = CAST(:uid AS uuid) AND dedup_hash = :hash FOR UPDATE")
_DELIVER_SQL = f"""
    UPDATE results r SET enrichment_data =
      ((r.enrichment_data::jsonb - '{DELIVERY_EXCLUDED_KEY}') || CAST(:late AS jsonb))::json
    WHERE r.id = CAST(:rid AS uuid) AND r.user_id = CAST(:uid AS uuid) AND {_HELD}
"""  # noqa: S608 -- splices only module constants; every value is bound


def deliver_completed_held_rows(*, batch: int = _BATCH_ROWS) -> dict:
    """One bounded tick. Returns counts (also read by the tests); never raises for one row."""
    from src.db.session import rls_sync_session, system_sync_session

    stats = {"candidates": 0, "delivered": 0, "no_quota": 0, "blocked": 0, "stale": 0,
             "errors": 0, "privacy_withheld": 0, "skipped": ""}
    if not settings.CV_LATE_DELIVERY_ENABLED:
        stats["skipped"] = "CV_LATE_DELIVERY_ENABLED is off"
        return stats
    from src.privacy.authorize import RuleSnapshotError, load_rule_snapshot

    with system_sync_session() as db:
        # The privacy rules before any delivery (Phase 6 P5g): a held row a rule
        # withholds is flagged, never charged. Unreadable rules: the whole tick does
        # nothing (no delivery, contact request, dialer push or notice; Codex), and
        # still reports every count.
        try:
            privacy = load_rule_snapshot(db)
        except RuleSnapshotError as exc:
            db.rollback()
            _logger.error("Late delivery: privacy rules unavailable: %s", exc)
            return {**stats, "skipped": "privacy rules unavailable",
                    "contacts_requested": 0, "dialer_pushed": 0, "notified_accounts": 0}
        rows = db.execute(sa_text(_CANDIDATES_SQL),
                          {"batch": batch, "per_account": _PER_ACCOUNT_ROWS}).mappings().all()
        db.rollback()
    stats["candidates"] = len(rows)
    by_account: dict[str, list] = {}
    for row in rows:
        by_account.setdefault(row["user_id"], []).append(row)
    # Every charge and unhold runs in the account's own RLS session (as a scrape job's
    # reservation does), with explicit user_id predicates as well.
    for user_id, account_rows in by_account.items():
        with rls_sync_session(user_id) as db:
            for row in account_rows:
                outcome = _deliver_one(db, row, privacy)
                stats[outcome] += 1
                if outcome in ("no_quota", "blocked"):
                    break                        # the rest of this account cannot be charged either
    if stats["candidates"]:
        _logger.info("Late delivery: %d candidate(s): %d delivered, %d no quota, %d blocked, "
                     "%d stale, %d error(s)", stats["candidates"], stats["delivered"],
                     stats["no_quota"], stats["blocked"], stats["stale"], stats["errors"])
    # Every tick: also picks up rows a crashed tick delivered and never asked or announced.
    try:
        stats["contacts_requested"] = request_late_contacts()
    except (SoftTimeLimitExceeded, TimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001 -- deliveries are committed; retried next tick
        _logger.warning("Late delivery contact requests failed: %s", type(exc).__name__)
    try:
        stats["dialer_pushed"] = push_late_dialer()
    except (SoftTimeLimitExceeded, TimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001 -- deliveries are committed; retried next tick
        _logger.warning("Late delivery dialer pushes failed: %s", type(exc).__name__)
    try:
        stats["notified_accounts"] = notify_late_deliveries()
    except (SoftTimeLimitExceeded, TimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001 -- deliveries are committed; the notice retries next tick
        _logger.warning("Late delivery notices failed: %s", type(exc).__name__)
    return stats


# Rows delivered but not announced yet, in the same scope as discovery (done code-violation
# jobs of the row's own account), so neither query reaches across record types or accounts.
# A row withheld by a privacy rule after its late delivery is neither announced nor sent for
# contacts (here and _CONTACTS_PENDING_SQL). The filter is in the WHERE, before any LIMIT,
# so withheld rows take no batch slot; their markers stay pending, so lifting a rule would
# resume them.
_PENDING_SCOPE = f"""
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id AND j.status = 'done'
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
                           AND sc.record_type = 'code_violation'
    WHERE r.enrichment_data::jsonb->'{LATE_KEY}'->>'notified' = 'false'
      AND {not_privacy_suppressed_sql('r')}
"""  # noqa: S608 -- splices only module constants
# Oldest pending delivery first, so a backlog drains in order and no account is skipped
# every tick (Codex C3 P2).
_PENDING_ACCOUNTS_SQL = f"""
    SELECT r.user_id::text AS user_id
    {_PENDING_SCOPE}
    GROUP BY r.user_id
    ORDER BY min(r.enrichment_data::jsonb->'{LATE_KEY}'->>'delivered_at'), r.user_id
    LIMIT :limit
"""  # noqa: S608 -- splices only module constants; every value is bound
_PENDING_ROWS_SQL = f"""
    SELECT r.id::text AS id, r.job_id::text AS job_id
    {_PENDING_SCOPE}
      AND r.user_id = CAST(:uid AS uuid)
    ORDER BY r.enrichment_data::jsonb->'{LATE_KEY}'->>'delivered_at', r.id
    FOR UPDATE OF r
"""  # noqa: S608 -- splices only module constants; every value is bound
_MARK_NOTIFIED_SQL = f"""
    UPDATE results SET enrichment_data =
      jsonb_set(enrichment_data::jsonb, '{{{LATE_KEY},notified}}', 'true'::jsonb)::json
    WHERE user_id = CAST(:uid AS uuid) AND id = ANY(CAST(:ids AS uuid[]))
"""  # noqa: S608 -- splices only module constants; every value is bound
# Late rows whose run had skip tracing on and that were not asked yet, per job.
_CONTACTS_PENDING_SQL = f"""
    SELECT r.job_id::text AS job_id, r.user_id::text AS user_id, array_agg(r.id::text) AS ids
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id AND j.status = 'done'
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
                           AND sc.record_type = 'code_violation'
    WHERE r.is_duplicate = false
      AND r.enrichment_data::jsonb->'{LATE_KEY}'->>'{CONTACTS_KEY}' = '{CONTACTS_PENDING}'
      AND {not_privacy_suppressed_sql('r')}
    GROUP BY r.job_id, r.user_id
    ORDER BY min(r.enrichment_data::jsonb->'{LATE_KEY}'->>'delivered_at')
    LIMIT :limit
"""  # noqa: S608 -- splices only module constants; every value is bound
_MARK_CONTACTS_SQL = f"""
    UPDATE results SET enrichment_data =
      jsonb_set(enrichment_data::jsonb, '{{{LATE_KEY},{CONTACTS_KEY}}}', to_jsonb(CAST(:state AS text)))::json
    WHERE user_id = CAST(:uid AS uuid) AND job_id = CAST(:jid AS uuid) AND id = ANY(CAST(:ids AS uuid[]))
      AND enrichment_data::jsonb->'{LATE_KEY}'->>'{CONTACTS_KEY}' = '{CONTACTS_PENDING}'
"""  # noqa: S608 -- splices only module constants; every value is bound
_CONTACT_JOBS = 50
# The enqueue's own outcomes (as degradation_repair reads them): wait and try next tick,
# or a gate that stands.
_CONTACTS_WAIT = {"disabled", "no_token", "unenforced"}
_CONTACTS_SUPPRESS = {"config_off", "starter", "not_owner"}


def request_late_contacts() -> int:
    """Ask the run's paid skip trace for late rows whose scraper has it on; returns the
    number of jobs asked.

    The SAME enqueue a run uses (_enqueue_skip_trace_rows), narrowed to this job's
    pending late rows (only_result_ids) and quiet (a done job's log is not appended to):
    every spend gate, the per-job advisory lock, the claim (access, trial allowance,
    freeze/end, account limits) and the dispatcher apply unchanged. A run's other
    untraced rows are never queued by this. Each row is asked once: "requested" once
    the enqueue ran, "not_requested" for a gate that stands; a waiting gate (skip trace
    switched off globally, claims paused) leaves it pending for the next tick.
    In the account's own RLS session, like degradation_repair's done-job enqueue.
    """
    from src.db.models import Job, ScraperConfig
    from src.db.session import rls_sync_session, system_sync_session
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows
    from src.workers.tasks_helpers.status import _redis

    with system_sync_session() as db:
        jobs = db.execute(sa_text(_CONTACTS_PENDING_SQL), {"limit": _CONTACT_JOBS}).mappings().all()
        db.rollback()
    if not jobs:
        return 0
    r = _redis()
    asked = 0
    for row in jobs:
        params = {"uid": row["user_id"], "jid": row["job_id"], "ids": list(row["ids"])}
        with rls_sync_session(row["user_id"]) as db:
            try:
                job = db.get(Job, row["job_id"])
                config = db.get(ScraperConfig, job.scraper_config_id) if job else None
                if job is None or config is None or str(job.user_id) != row["user_id"]:
                    db.rollback()
                    continue
                outcome = _enqueue_skip_trace_rows(db, job, r, row["job_id"], config,
                                                   only_result_ids=list(row["ids"]), quiet=True)
                if outcome in _CONTACTS_WAIT:
                    db.rollback()
                    continue
                state = "not_requested" if outcome in _CONTACTS_SUPPRESS else "requested"
                db.execute(sa_text(_MARK_CONTACTS_SQL), {**params, "state": state})
                db.commit()
                asked += state == "requested"
            except (SoftTimeLimitExceeded, TimeLimitExceeded):
                db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 -- one job never stops the others
                db.rollback()
                _logger.warning("Late contact request failed for job %s: %s", str(row["job_id"])[:8],
                                type(exc).__name__)
    return asked


# Late rows waiting for their dialer decision, oldest first.
_DIALER_PENDING_SQL = f"""
    SELECT r.id::text AS id, r.user_id::text AS user_id, r.job_id::text AS job_id
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id AND j.status = 'done'
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
                           AND sc.record_type = 'code_violation'
    WHERE r.is_duplicate = false
      AND r.enrichment_data::jsonb->'{LATE_KEY}'->>'{DIALER_KEY}' = '{DIALER_PENDING}'
    ORDER BY r.enrichment_data::jsonb->'{LATE_KEY}'->>'delivered_at', r.id
    LIMIT :limit
"""  # noqa: S608 -- splices only module constants; every value is bound
_DIALER_LOCK_JOB_SQL = """
    SELECT j.dialer_pushed_at, sc.id AS config_id, lower(btrim(u.plan)) AS plan
    FROM jobs j
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
    JOIN users u ON u.id = j.user_id
    WHERE j.id = CAST(:jid AS uuid) AND j.user_id = CAST(:uid AS uuid) AND j.status = 'done'
    FOR UPDATE OF j
"""
_DIALER_LOCK_ROW_SQL = f"""
    SELECT r.enrichment_data::jsonb->'{LATE_KEY}'->>'delivered_at' AS delivered_at
    FROM results r
    WHERE r.id = CAST(:rid AS uuid) AND r.user_id = CAST(:uid AS uuid) AND r.job_id = CAST(:jid AS uuid)
      AND r.enrichment_data::jsonb->'{LATE_KEY}'->>'{DIALER_KEY}' = '{DIALER_PENDING}'
    FOR UPDATE OF r
"""  # noqa: S608 -- splices only module constants; every value is bound
_TRACE_IN_FLIGHT_SQL = """
    SELECT EXISTS (SELECT 1 FROM pending_skip_trace_rows p
                   WHERE p.result_id = CAST(:rid AS uuid) AND p.user_id = CAST(:uid AS uuid)
                     AND p.status IN ('queued', 'submitting', 'submitted'))
"""
_MARK_DIALER_SQL = f"""
    UPDATE results SET enrichment_data =
      jsonb_set(enrichment_data::jsonb, '{{{LATE_KEY},{DIALER_KEY}}}', to_jsonb(CAST(:state AS text)))::json
    WHERE id = CAST(:rid AS uuid) AND user_id = CAST(:uid AS uuid) AND job_id = CAST(:jid AS uuid)
"""  # noqa: S608 -- splices only module constants; every value is bound
_DIALER_ROWS = 100
# A dialer wants the row's contacts: wait for its own skip trace, but never longer than this.
_DIALER_WAIT = timedelta(hours=24)
# The run's sweep reads its rows right after it claims the job; a row delivered late within this
# margin of that claim may already be in that push, so it is not pushed again.
_SWEEP_OVERLAP = timedelta(minutes=10)


def push_late_dialer() -> int:
    """Send late rows to the run's dialer, once each; returns the number pushed.

    Per row, in the account's RLS session, under the job lock then the row lock:
      * the run's dialer push has not run yet: "via_sweep" (the sweep sends the row,
        which is a lead of the run now);
      * the run was pushed within _SWEEP_OVERLAP of this delivery: "skipped_overlap"
        (that push may already carry it; a duplicate import cannot be undone);
      * no Business+ plan or no dialer destination now: "not_eligible";
      * the row's own skip trace is in flight: wait, at most _DIALER_WAIT;
      * the sweep's full predicate for this one row (dialer_ready_conditions with unknown
        DNC, not a duplicate, the 18-month tax cap, actionable): otherwise "not_ready";
      * then claim FIRST ("pushed", committed), send after: an outbox vendor gets the one
        row (idempotent per job and result) and the outbox drains; a generic webhook gets
        one id-only push. A send that raises before anything is sent puts the row back to
        "pending" (the sweep's un-claim). Replay of a failed delivery stays the user's action.

    Privacy (Phase 6 P4b): the rules load once, before any row is claimed; if they cannot,
    nothing is touched this tick. A row the rules withhold (or hold for review) ends
    "privacy_withheld" and is never sent; the send paths re-check it anyway.
    """
    from sqlalchemy import select

    from src.api.dialer_filters import dialer_ready_conditions
    from src.api.lead_actionability import actionable_condition
    from src.api.tax_filters import tax_cap_condition
    from src.config.constants import BUSINESS_FEATURES_PLANS
    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import rls_sync_session, system_sync_session
    from src.privacy.authorize import RuleSnapshotError, authorize, load_rule_snapshot

    with system_sync_session() as db:
        rows = db.execute(sa_text(_DIALER_PENDING_SQL), {"limit": _DIALER_ROWS}).mappings().all()
        if not rows:
            return 0
        try:
            snapshot = load_rule_snapshot(db)
        except RuleSnapshotError as exc:
            db.rollback()
            _logger.error("Late dialer push: privacy rules unavailable (%s); nothing claimed",
                          str(exc)[:160])
            return 0
        db.rollback()
    pushed = 0
    for row in rows:
        params = {"rid": row["id"], "uid": row["user_id"], "jid": row["job_id"]}
        with rls_sync_session(row["user_id"]) as db:
            try:
                run = db.execute(sa_text(_DIALER_LOCK_JOB_SQL), params).mappings().first()
                if run is None:
                    db.rollback()
                    continue
                locked = db.execute(sa_text(_DIALER_LOCK_ROW_SQL), params).first()
                if locked is None or not locked.delivered_at:
                    db.rollback()
                    continue
                delivered_at = datetime.fromisoformat(locked.delivered_at)
                config = db.get(ScraperConfig, run["config_id"])
                state = None
                if run["plan"] not in BUSINESS_FEATURES_PLANS or not has_dialer_destination(
                        config.deliver if config else None):
                    state = "not_eligible"
                elif run["dialer_pushed_at"] is None:
                    state = "via_sweep"
                elif not uses_outbox(config.deliver):
                    # A generic webhook keeps no per-lead record, so the run's push is judged by
                    # time (Codex D2 P1): delivered before the run's claim, the sweep read it;
                    # inside the margin, unknown: skipped, never risked twice (the sweep's own
                    # at-most-once rule: a lost push is recoverable by export, a duplicate CRM
                    # import is not). An outbox vendor needs neither: its insert is idempotent
                    # per job and result, so a lead the sweep already queued is a no-op.
                    if delivered_at < run["dialer_pushed_at"]:
                        state = "via_sweep"
                    elif delivered_at <= run["dialer_pushed_at"] + _SWEEP_OVERLAP:
                        state = "skipped_overlap"
                if state is None:
                    in_flight = db.execute(sa_text(_TRACE_IN_FLIGHT_SQL), params).scalar()
                    if in_flight and datetime.now(UTC) - delivered_at < _DIALER_WAIT:
                        db.rollback()
                        continue                         # its contacts are still coming
                    lead = db.execute(select(Result).where(
                        Result.id == row["id"], Result.job_id == row["job_id"],
                        Result.user_id == row["user_id"], Result.is_duplicate.is_(False),
                        *dialer_ready_conditions(include_unknown_dnc=True),
                        tax_cap_condition(datetime.now(UTC).date()), actionable_condition(),
                    )).scalars().first()
                    if lead is None:
                        state = "not_ready"
                    elif not authorize(snapshot, lead, record_type=config.record_type,
                                       county=config.county, state=config.state).deliverable:
                        state = "privacy_withheld"
                if state is not None:
                    db.execute(sa_text(_MARK_DIALER_SQL), {**params, "state": state})
                    db.commit()
                    continue
                # Claim first, then send (the sweep's at-most-once order).
                db.execute(sa_text(_MARK_DIALER_SQL), {**params, "state": "pushed"})
                db.commit()
                try:
                    _send_to_dialer(db, db.get(Job, row["job_id"]), config, lead)
                except Exception:
                    # Outbox vendor: the insert is idempotent, so try again next tick. Generic
                    # webhook: the publish may have gone out before the error, so never again.
                    db.rollback()
                    retry = uses_outbox(config.deliver)
                    db.execute(sa_text(_MARK_DIALER_SQL),
                               {**params, "state": DIALER_PENDING if retry else "send_failed"})
                    db.commit()
                    raise
                pushed += 1
            except (SoftTimeLimitExceeded, TimeLimitExceeded):
                db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 -- one row never stops the others
                db.rollback()
                _logger.warning("Late dialer push failed for row %s: %s", str(row["id"])[:8],
                                type(exc).__name__)
    return pushed


def _send_to_dialer(db, job, config, row) -> None:
    """The sweep's dispatch (scheduler_helpers/dialer.py) for ONE lead: the outbox for a
    native vendor, one id-only generic-webhook push otherwise. Both authorize the row
    against the privacy rules immediately before sending (Phase 6 P4b)."""
    from src.workers.dialer_connectors import get_connector
    from src.workers.dialer_outbox import process_dialer_outbox
    from src.workers.scheduler_helpers.dialer import _materialize_dialer_outbox
    from src.workers.webhook_delivery import deliver_dialer_push

    deliver = config.deliver or {}
    connector = get_connector(deliver.get("dialer_type"))
    if connector.uses_outbox:
        _materialize_dialer_outbox(db, job, config, connector.VENDOR_ID, [{"id": row.id}])
        db.commit()
        process_dialer_outbox.delay(str(job.id))
        return
    if not deliver.get("dialer_webhook_url"):
        raise RuntimeError("generic dialer webhook without a URL")
    deliver_dialer_push.delay(str(job.id), result_ids=[str(row.id)], total=1)


_NOTIFY_ACCOUNTS = 500
_JOBS_IN_NOTICE = 5


def notify_late_deliveries() -> int:
    """One in-app notice per account for every delivered row not announced yet; returns
    the number of accounts announced.

    Exactly once: per account, ONE system-session transaction takes an advisory lock on
    the account (two ticks never split or double an account's batch), locks the pending
    rows, adds the notification (notification_emit.add_notification: type and prefs
    rules, no commit) and marks those rows notified, then commits. A crash before the
    commit leaves the rows pending and nothing written; after it, nothing is pending.
    Prefs off: the rows are marked and no notice is added. Ids and counts only.
    """
    from src.config.constants import NotificationType
    from src.db.session import system_sync_session
    from src.workers.notification_emit import add_notification

    with system_sync_session() as db:
        accounts = [r[0] for r in db.execute(sa_text(_PENDING_ACCOUNTS_SQL), {"limit": _NOTIFY_ACCOUNTS})]
        db.rollback()
    announced = 0
    for user_id in accounts:
        with system_sync_session() as db:
            try:
                db.execute(sa_text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                           {"k": f"cv_late_notify:{user_id}"})
                rows = db.execute(sa_text(_PENDING_ROWS_SQL), {"uid": user_id}).mappings().all()
                if not rows:
                    db.rollback()
                    continue
                job_ids = list(dict.fromkeys(r["job_id"] for r in rows))
                added = add_notification(
                    db, user_id=user_id, type=NotificationType.LATE_DELIVERY.value,
                    job_id=job_ids[0] if len(job_ids) == 1 else None,
                    detail={"count": len(rows), "job_ids": job_ids[:_JOBS_IN_NOTICE]})
                db.execute(sa_text(_MARK_NOTIFIED_SQL), {"uid": user_id, "ids": [r["id"] for r in rows]})
                db.commit()
                announced += int(added)
            except (SoftTimeLimitExceeded, TimeLimitExceeded):
                db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001 -- one account never stops the others
                db.rollback()
                _logger.warning("Late delivery notice failed for account %s: %s", str(user_id)[:8],
                                type(exc).__name__)
    return announced


def _deliver_one(db, row, privacy) -> str:
    """Deliver and charge one row in its own transaction; the stats key for the outcome.

    The row is authorized under `privacy` while locked and BEFORE any charge: a row a
    rule withholds is flagged and never charged (`privacy_withheld`); a rule error rolls
    the row back like any other failure."""
    from src.privacy.enforce import enforce_rows
    from src.workers.tasks import grant_records

    params = {"rid": row["id"], "uid": row["user_id"], "jid": row["job_id"]}
    try:
        db.execute(sa_text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}'"))
        run = db.execute(sa_text(_LOCK_JOB_SQL), params).first()
        if run is None:
            db.rollback()
            return "stale"
        locked = db.execute(sa_text(_LOCK_ROW_SQL), params).first()
        if locked is None or not locked.dedup_hash:
            db.rollback()
            return "stale"
        holder = db.execute(sa_text(_LOCK_CLAIM_SQL),
                            {"uid": row["user_id"], "hash": locked.dedup_hash}).scalar()
        if holder != row["id"]:
            db.rollback()                        # a later run took the claim and delivered it
            return "stale"
        if enforce_rows(db, privacy, [row["id"]]).flagged:
            db.commit()                          # flagged: never charged, never delivered
            return "privacy_withheld"
        granted, window_start, at, block = grant_records(
            db, user_id=row["user_id"], want=1, unlimited_grants=True)
        if granted < 1:
            db.rollback()                        # stays held; tried again next tick
            return "blocked" if block else "no_quota"
        late = {LATE_KEY: {"delivered_at": at.isoformat(), "quota_period_start":
                           window_start.isoformat() if window_start else None,
                           "policy": POLICY_CODE_VIOLATION,
                           # Announced by notify_late_deliveries, exactly once.
                           "notified": False}}
        if run.skip_trace_enabled:
            # The run would have traced it: request_late_contacts asks the same enqueue,
            # for this row only, once (late delivery D1).
            late[LATE_KEY][CONTACTS_KEY] = CONTACTS_PENDING
        if has_dialer_destination(run.deliver):
            # The run pushes to a dialer: push_late_dialer decides, once (late delivery D2).
            late[LATE_KEY][DIALER_KEY] = DIALER_PENDING
        written = db.execute(sa_text(_DELIVER_SQL), {**params, "late": json.dumps(late)}).rowcount
        if written != 1:
            db.rollback()
            return "stale"
        db.commit()
        return "delivered"
    except (SoftTimeLimitExceeded, TimeLimitExceeded):
        db.rollback()
        raise                                    # the worker is ending this task
    except Exception as exc:  # noqa: BLE001 -- one row's failure never stops the tick
        db.rollback()
        _logger.warning("Late delivery failed for row %s: %s", str(row["id"])[:8],
                        type(exc).__name__)
        return "errors"


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.cv_late_delivery.deliver_completed_held_rows_task")
    def deliver_completed_held_rows_task() -> dict:
        """Beat entry point: see deliver_completed_held_rows."""
        return deliver_completed_held_rows()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.error("Late delivery task NOT registered: %s", str(exc)[:120], exc_info=exc)
