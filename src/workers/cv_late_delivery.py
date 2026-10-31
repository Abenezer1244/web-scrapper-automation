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

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy import text as sa_text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, NOT_QUALIFIED
from src.api.lead_qualification import POLICY_CODE_VIOLATION, POLICY_KEY, qualified_sql
from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.cv_late_delivery")

LATE_KEY = "late_delivery"
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
_ELIGIBLE = (f"r.is_duplicate = false AND r.duplicate_reason IS NULL "
             f"AND jsonb_typeof(r.enrichment_data::jsonb) = 'object' AND {_HELD} "
             f"AND {qualified_sql('r')}")

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

_LOCK_JOB_SQL = ("SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
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
             "errors": 0, "skipped": ""}
    if not settings.CV_LATE_DELIVERY_ENABLED:
        stats["skipped"] = "CV_LATE_DELIVERY_ENABLED is off"
        return stats
    with system_sync_session() as db:
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
                outcome = _deliver_one(db, row)
                stats[outcome] += 1
                if outcome in ("no_quota", "blocked"):
                    break                        # the rest of this account cannot be charged either
    if stats["candidates"]:
        _logger.info("Late delivery: %d candidate(s): %d delivered, %d no quota, %d blocked, "
                     "%d stale, %d error(s)", stats["candidates"], stats["delivered"],
                     stats["no_quota"], stats["blocked"], stats["stale"], stats["errors"])
    # Every tick, not only after a delivery: it also announces rows a crashed tick
    # delivered and never announced.
    try:
        stats["notified_accounts"] = notify_late_deliveries()
    except (SoftTimeLimitExceeded, TimeLimitExceeded):
        raise
    except Exception as exc:  # noqa: BLE001 -- deliveries are committed; the notice retries next tick
        _logger.warning("Late delivery notices failed: %s", type(exc).__name__)
    return stats


# Accounts with delivered rows not announced yet. Scoped like discovery (done code-violation
# jobs of the same account), so it never scans results across record types.
_PENDING_ACCOUNTS_SQL = f"""
    SELECT DISTINCT r.user_id::text AS user_id
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = r.user_id AND j.status = 'done'
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = j.user_id
                           AND sc.record_type = 'code_violation'
    WHERE r.enrichment_data::jsonb->'{LATE_KEY}'->>'notified' = 'false'
    LIMIT :limit
"""  # noqa: S608 -- splices only module constants; every value is bound
_PENDING_ROWS_SQL = f"""
    SELECT r.id::text AS id, r.job_id::text AS job_id
    FROM results r
    WHERE r.user_id = CAST(:uid AS uuid)
      AND r.enrichment_data::jsonb->'{LATE_KEY}'->>'notified' = 'false'
    ORDER BY r.enrichment_data::jsonb->'{LATE_KEY}'->>'delivered_at', r.id
    FOR UPDATE OF r
"""  # noqa: S608 -- splices only module constants; every value is bound
_MARK_NOTIFIED_SQL = f"""
    UPDATE results SET enrichment_data =
      jsonb_set(enrichment_data::jsonb, '{{{LATE_KEY},notified}}', 'true'::jsonb)::json
    WHERE user_id = CAST(:uid AS uuid) AND id = ANY(CAST(:ids AS uuid[]))
"""  # noqa: S608 -- splices only module constants; every value is bound
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


def _deliver_one(db, row) -> str:
    """Deliver and charge one row in its own transaction; the stats key for the outcome."""
    from src.workers.tasks import grant_records

    params = {"rid": row["id"], "uid": row["user_id"], "jid": row["job_id"]}
    try:
        db.execute(sa_text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}'"))
        if db.execute(sa_text(_LOCK_JOB_SQL), params).first() is None:
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
