"""Billing and the done transition of a scrape run, as one production helper.

Moved verbatim out of ``run_scrape_job`` (tasks.py) so the tests drive the code
production runs: that function needs Redis and a browser, so its finalization could
only be pinned by reading its source. Everything from the force-finalize guard to the
done-CAS commit lives here; what run_scrape_job does after a completed run (logs, the
done publish, notification, email, webhook, dialer) stays there and runs only on
``FinalizeKind.DONE``.
"""
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text as sa_text

from src.api.lead_actionability import actionable_sql
from src.api.quota_window import (
    reservation_is_current_sql,
    window_cte_sql,
    window_set_sql,
)
from src.db.models import Job
from src.utils.logger import setup_logger
from src.workers.tasks_helpers.status import (
    _TERMINAL_STATUSES,
    _attempt_clauses,
    _fail_job,
    _now,
    _set_stage,
    _set_status,
    attempt_state,
    finalize_exit,
)

_logger = setup_logger("workers.tasks")


class FinalizeKind(str, Enum):
    DONE = "done"                          # billed (or already billed) and marked done
    ALREADY_TERMINAL = "already_terminal"  # cancelled/failed/done under us: nothing billed
    BILLING_FAILED = "billing_failed"      # the user counter did not move; job failed
    LOST_OWNERSHIP = "lost_ownership"      # another attempt holds the job: did nothing


@dataclass(frozen=True)
class FinalizeOutcome:
    kind: FinalizeKind
    display_count: int = 0


def _alert_dedup_release_failed(job_id: str, user_id, context: str, exc: Exception) -> None:
    """A dedup-claim release failed — escalate, never just log.

    ``user_id`` MUST be a plain value (the cached ``_boot_user_id``), never an ORM
    attribute: every call site runs after ``db.rollback()``, which expires ORM
    instances, so reading ``job.user_id`` there would emit a refresh SELECT on the
    session that just failed. If that raised, it would escape the except block and
    skip the job's real failure handling — an alert must never replace it (Codex).

    Releasing `delivered_records` is what keeps a lead that was NEVER delivered
    and NEVER billed from being treated as an already-seen duplicate forever. If
    the release fails, those leads become permanently unreachable for that user:
    excluded from every future run's results and downloads, silently, while the
    job tells them "no file was delivered and you were not charged".

    That failure mode was invisible for exactly this reason — the call sites
    caught the exception and logged it. In production the worker role was missing
    DELETE on delivered_records, so all five release paths raised
    InsufficientPrivilege, stranding 16,761 claims with nothing but a log line
    that had already scrolled out of retention by the time anyone looked.

    send_ops_alert persists a durable row even when OPS_ALERT_EMAIL is unset
    (which it is in prod), so the incident survives log rotation.
    """
    _logger.error(
        "Job %s: dedup-claim release FAILED (%s) — leads may be permanently "
        "suppressed as duplicates: %s", job_id, context, str(exc)[:200],
    )
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "dedup_release_failed",
            f"{context}:{job_id}",
            "Dedup-claim release failed. Leads may be permanently suppressed",
            f"Job {job_id} (user {user_id}) could not release its delivered_records "
            f"claims on the '{context}' path: {str(exc)[:400]}. "
            "Those leads were not delivered and not billed, but they remain claimed, "
            "so future runs will drop them as duplicates. Check that the worker role "
            "still holds DELETE on delivered_records "
            "(scripts/_cutover_step2_grants_policies.py), then release them with: "
            "DELETE FROM delivered_records WHERE first_job_id = <job> AND user_id = <user>;",
        )
    except Exception as alert_exc:  # noqa: BLE001 — alerting must never mask the original
        _logger.error("Job %s: dedup-release alert failed too: %s", job_id, str(alert_exc)[:160])


def _release_claims_of_cancelled_job(db, job_id: str, user_id) -> None:
    """Release every claim a job holds once it has been cancelled mid-run.

    A cancelled run delivers nothing and bills nothing, yet the two paths that
    notice a cancellation (the force-finalize guard before billing, and the
    done-CAS losing to a cancel) returned with the job's claims still in place:
    the ones its dedup step wrote and any it took over from an earlier run. Every
    later run then hid those leads as "already delivered" with nothing ever
    delivered. Same defect class as the post-crash cleanup fixed on 2026-09-08,
    on the two exits that still had it (Codex).

    ``user_id`` must be a plain value, for the reason in _alert_dedup_release_failed.

    The job's state is re-checked INSIDE the delete, not trusted from the caller:
    both callers only know the job is terminal, and 'done' is terminal too. A
    stale attempt overlapping a watchdog retry that already completed and billed
    would otherwise strip a finished job's claims, and the next run would deliver
    and bill those properties again (Codex). Only a job that is cancelled and was
    never billed gives its claims up, and only if it was created after billing
    was stamped: a requeued older job's NULL stamp proves nothing (Codex review
    round 7).
    """
    from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE

    try:
        db.execute(
            sa_text(
                "DELETE FROM delivered_records "
                "WHERE first_job_id = :jid AND user_id = CAST(:uid AS uuid) "
                "  AND EXISTS (SELECT 1 FROM jobs j "
                "              WHERE j.id = :jid AND j.user_id = CAST(:uid AS uuid) "
                "                AND j.status = 'cancelled' "
                "                AND j.billing_applied_at IS NULL "
                "                AND j.created_at >= :since)"
            ),
            {"jid": job_id, "uid": str(user_id), "since": BILLING_STAMP_RELIABLE_SINCE},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        _alert_dedup_release_failed(job_id, user_id, "cancelled", exc)


def finalize_billing_and_done(
    db, r, *, job, user, config, job_id: str, attempt_token, object_key,
    boot_user_id,
) -> FinalizeOutcome:
    """Bill the run and mark it done in ONE transaction, or say why not.

    ``boot_user_id`` is the plain user id cached before any rollback (see
    _alert_dedup_release_failed). Returns DONE with the count the customer was
    charged for; any other kind means run_scrape_job must stop."""
    _boot_user_id = boot_user_id
    # Force-finalize guard (Codex P2): a batch force-finalize may have
    # cancelled this child while it was exporting. Re-check the live DB
    # status before charging quota — never bill a job that is no longer
    # ours to complete. (A cancel landing between this check and the final
    # done-CAS still can't resurrect the job; at worst that sliver of a
    # window bills records that were genuinely scraped.)
    db.refresh(job)
    if job.status in _TERMINAL_STATUSES:
        _logger.info(
            "Job %s externally terminalized (%s) after export — skipping billing/delivery",
            job_id, job.status,
        )
        _terminal_cleanup(db, job_id, _boot_user_id)
        return FinalizeOutcome(FinalizeKind.ALREADY_TERMINAL)

    # Last stage boundary. It goes HERE, before the billing reads open the
    # transaction that the done-CAS commits with, and it commits on its own:
    # everything below this line is deliberately held uncommitted so billing and
    # the terminal transition land together, and a stage write inside that window
    # — with commit=True, or with commit=False and someone else's commit arriving
    # first — would split them, which is the crash that leaves a job billed but
    # not done (Codex P1).
    if not _set_stage(db, job, "finalizing", expected_started_at=attempt_token):
        # False is a CAS miss OR a swallowed telemetry error (_set_progress never
        # raises). Ask the row which: terminal -> today's force-finalize handling;
        # another attempt -> stop; still ours -> it was telemetry, carry on.
        stop = _fenced_exit(db, job_id, _boot_user_id, attempt_token, "stage")
        if stop is not None:
            if stop.kind is FinalizeKind.ALREADY_TERMINAL:
                _terminal_cleanup(db, job_id, _boot_user_id)
            return stop
        db.rollback()  # still ours: end the check's transaction, keep going

    # The billing transaction opens HERE, with this attempt's ownership read under a
    # row lock that is held through the billing reads, the billing CAS, the users
    # settle and the done-CAS, which commit together. A re-queue or cancel issued in
    # that window waits, then finds the job finished. Not ours -> nothing is billed.
    stop = _fenced_exit(db, job_id, _boot_user_id, attempt_token, "billing")
    if stop is not None:
        if stop.kind is FinalizeKind.ALREADY_TERMINAL:
            _terminal_cleanup(db, job_id, _boot_user_id)
        return stop

    # Atomic update of monthly record usage.
    # Sprint 6.4: duplicates delivered to this user in a prior scrape
    # do NOT count against the monthly quota.
    # 2026-09-02 (owner decision): rows with no property AND no mailing
    # address are not leads and are NOT billed either — which is why this
    # whole block now runs AFTER inline enrichment (addresses for many
    # counties only exist post-enrichment) and right before the done-CAS.
    # The force-finalize guard above moved with it (Codex).
    #
    # Bill the PERSISTED billable set, not len(records): with conflict-skipping
    # inserts the in-memory scrape count can diverge from what actually landed
    # (intra-run fingerprint collisions, a re-run over a changed source set), so
    # the authoritative billable count is this job's non-duplicate result rows
    # (no-dedup_hash rows have is_duplicate=false, so they're included). (Codex)
    billable_count = db.execute(
        sa_text(
            "SELECT count(*) FROM results "
            "WHERE job_id = :jid AND user_id = CAST(:uid AS uuid) AND is_duplicate = false "
            f"AND {actionable_sql('results')}"
        ),
        {"jid": job_id, "uid": str(job.user_id)},
    ).scalar() or 0
    from sqlalchemy import update as sa_update
    # Idempotent billing (migration 063): claim billing for THIS job via a CAS
    # on billing_applied_at. Only the attempt that flips it from NULL bills the
    # user, so a watchdog re-run (which re-reaches this point) never
    # double-charges records_used. billed_count records the charged amount. The
    # Job CAS + the User increment commit together (a crash between the two
    # execute()s rolls both back — neither is committed until db.commit()).
    # ONE database-clock reading, reused as the billing instant for BOTH the
    # job anchor and the user's period decision below.
    #
    # Postgres NOW() is transaction_timestamp() — fixed when the transaction
    # OPENED, which here is the billable-count SELECT above, potentially
    # minutes earlier. A transaction that opens just before a UTC month
    # boundary and reaches this point just after it would stamp
    # billing_applied_at in the new month while NOW() still resolved to the
    # old one, leaving records_period_start stale. The beat task would then
    # read that user as stale and zero a charge that had just been applied
    # inside the new period — the exact failure this whole change exists to
    # prevent. clock_timestamp() reads the wall clock at statement time, and
    # binding the single value into both statements makes the job anchor and
    # the user period agree by construction rather than by luck. (Codex)
    _billed_at = db.execute(sa_text("SELECT clock_timestamp()")).scalar()
    billed_now = db.execute(
        sa_update(Job)
        .where(
            Job.id == job_id, Job.billing_applied_at.is_(None),
            *_attempt_clauses(attempt_token),
        )
        .values(billed_count=billable_count, billing_applied_at=_billed_at)
    ).rowcount
    if billed_now:
        # PERIOD-AWARE increment. The counter is rolled forward in the SAME
        # statement that charges it, so the month boundary is applied at the
        # moment of billing rather than whenever the daily beat task next
        # happens to run.
        #
        # This closes a real quota-loss hole: reset_monthly_usage runs daily
        # at 00:05 UTC to survive Beat downtime on the 1st, but it used to
        # zero records_used unconditionally — so a late catch-up run wiped
        # usage that had ALREADY been billed inside the new period. In prod a
        # user billed 67 records on Sep 2 and a Sep-3 catch-up run destroyed
        # them.
        #
        # It also makes the daily task's zeroing provably safe rather than
        # merely hopeful: because every bill advances records_period_start,
        # a period_start that is still stale when the beat runs PROVES no job
        # billed in the current period, so there is nothing of value to zero.
        #
        # One statement, evaluated atomically under the row lock Postgres
        # already takes for an UPDATE, so a concurrent bill for the same user
        # cannot interleave a read and a write (no lost update).
        # SETTLE THE DELTA, not the whole amount. The plan cap already
        # RESERVED this job's grant (migration 087) and charged it to
        # records_used at that moment, which is what stops two concurrent
        # jobs being allocated the same remaining quota. So the charge owed
        # here is only the difference between what was actually delivered
        # and what was held.
        #
        # For a job that delivered exactly its grant the delta is 0. For a
        # job that never reserved — an unlimited-plan user, whose cap block
        # is skipped entirely — reserved_count is 0 and the delta is the
        # full billable_count, so this one expression covers both. In-flight
        # jobs at deploy time also have reserved_count = 0 and bill exactly
        # as they did before.
        # Which period does this job's reservation belong to? A grant made
        # in an EARLIER period was charged to that period's counter, and
        # that counter has since rolled — so the charge is gone and the
        # delta is meaningless. The leads are being delivered NOW, so the
        # current period must carry them in full. Netting a stale grant off
        # instead would deliver records nobody is charged for. (Codex)
        # NULL-PERIOD RULE (must match src/api/quota.py::effective_records_used):
        # a NULL records_period_start never zeroes the COUNTER. It is
        # unreachable — migration 086 made the column NOT NULL with a
        # server_default — but the two halves used to disagree, and they
        # disagreed in the revenue-losing direction: the API gate preserved
        # the counter on NULL while the worker discarded it, silently handing
        # out a free period's quota. Only a genuinely STALE period zeroes.
        # The period column itself is still stamped on NULL (adopted), which
        # matches how the rollover treats it. (Codex)
        # WHICH ENTITLEMENT WINDOW does the reservation belong to, and is
        # that still the live one? Both questions are now answered INSIDE
        # the charging statement, under the users row lock, rather than by a
        # separate unlocked SELECT. That matters: the previous shape read
        # "is it current?" first and charged second, so a rollover landing
        # between the two would net a grant off a counter the rollover had
        # already zeroed — under-charging by exactly the reserved amount.
        #
        # jobs -> users lock order is preserved: this job's row was already
        # locked by the billing CAS above, and FOR UPDATE OF u takes only
        # the users row. Locking users first here would invert against a
        # concurrent watchdog re-run and deadlock.
        _settle = _settle_user_charge(db, job_id, billable_count, _billed_at)
        user_billed = 0 if _settle is None else 1
        if _settle is not None:
            _reserved = int(_settle.job_reserved or 0)
            if _reserved and not _settle.applied_reserved:
                # The grant was charged to a window that has since rolled and
                # been zeroed, so there is no charge left to net against. The
                # leads are being delivered NOW, so the live window carries
                # them in full rather than the customer receiving records
                # nobody is charged for.
                _logger.warning(
                    "Job %s: reservation of %d belongs to an earlier "
                    "entitlement window — charging the full delivered count "
                    "to the current one instead of netting a charge that has "
                    "already rolled",
                    job_id, _reserved,
                )
        if user_billed != 1:
            # The job was CAS-marked billed but the user counter did NOT move
            # (deleted user / bad id / RLS scope). Don't leave the job marked
            # billed-without-charge — roll back and fail loudly (Codex).
            db.rollback()
            reason = "Billing failed: user record-usage counter could not be updated."
            # The rollback released the row lock, so ask again before failing it.
            stop = _fenced_exit(db, job_id, _boot_user_id, attempt_token, "billing_failed")
            if stop is not None:
                if stop.kind is FinalizeKind.ALREADY_TERMINAL:
                    _terminal_cleanup(db, job_id, _boot_user_id)
                return stop
            db.rollback()  # still ours: _fail_job runs its own transaction
            if _fail_job(db, job, r, job_id, reason, expected_started_at=attempt_token):
                from src.workers.notification_emit import create_notification
                create_notification(
                    user_id=job.user_id, type="job_failed", job_id=job_id,
                    detail={
                        "scraper_name": getattr(config, "name", None),
                        "county": getattr(config, "county", None),
                        "error_summary": reason[:200],
                    },
                )
            return FinalizeOutcome(FinalizeKind.BILLING_FAILED)
    else:
        # The CAS also misses when this attempt no longer owns the job, so the row
        # decides (still under the billing lock): only an owner reaches the
        # already-billed path.
        stop = _fenced_exit(db, job_id, _boot_user_id, attempt_token, "billing_cas")
        if stop is not None:
            if stop.kind is FinalizeKind.ALREADY_TERMINAL:
                _terminal_cleanup(db, job_id, _boot_user_id)
            return stop
        _logger.info(
            "Job %s already billed (billing_applied_at set) — skipping "
            "records_used increment on this re-run", job_id,
        )
        # Re-run after a crash between the billing commit and the done-CAS:
        # the headline/email/webhook must report what was actually CHARGED,
        # not a fresh count that enrichment may have changed since (Codex).
        db.refresh(job)
        if job.billed_count is not None:
            billable_count = int(job.billed_count)

    # ── NOW mark done — in the SAME transaction as the billing writes ──────
    # record_count reflects unique (non-duplicate) leads — what the user
    # actually sees on the results page. The raw scrape total is in the
    # log: "{N} records saved ({unique} new leads, {dup} duplicates)".
    # Persisted non-duplicate ACTIONABLE rows — the same number that was just
    # billed, so the headline, email, webhook and bill can never disagree.
    # The billing CAS + records_used increment above are still UNCOMMITTED:
    # they commit together with this done-CAS, so a crash can never leave a
    # job billed-but-not-done (which a watchdog re-run would re-scrape and
    # re-export against a stale bill) or done-but-not-billed (Codex).
    display_count = int(billable_count)
    if not _set_status(
        db, job, "done",
        finished_at=_now(),
        record_count=display_count,
        export_key=object_key,
        commit=False,
        expected_started_at=attempt_token,
    ):
        # Cancelled (force-finalize) while enriching: the CAS kept the row
        # terminal — roll the pending billing back (a cancelled job is never
        # charged) and suppress the success log, email, and webhook.
        db.rollback()
        # Unless the job now belongs to another attempt: then NOTHING is released.
        # release_quota_reservation is not attempt-aware and would refund the grant
        # that attempt is reusing. (Unreachable while the billing lock is held; kept
        # so the done-CAS never depends on where it is called from.)
        stop = _fenced_exit(db, job_id, _boot_user_id, attempt_token, "done")
        if stop is not None and stop.kind is FinalizeKind.LOST_OWNERSHIP:
            return stop
        db.rollback()
        db.refresh(job)
        # The pending billing rolled back with that, but the plan cap's
        # RESERVATION was committed earlier in its own transaction and is
        # still charged to the user. A cancelled job delivers nothing, so
        # hand it back rather than leaving a permanent phantom charge.
        _terminal_cleanup(db, job_id, _boot_user_id)
        _logger.info(
            "Job %s externally terminalized (%s) — suppressing completion delivery",
            job_id, job.status,
        )
        return FinalizeOutcome(FinalizeKind.ALREADY_TERMINAL)
    db.commit()
    db.refresh(job)
    db.refresh(user)
    return FinalizeOutcome(FinalizeKind.DONE, int(display_count))


def _fenced_exit(db, job_id: str, boot_user_id, attempt_token, where: str) -> FinalizeOutcome | None:
    """Read this attempt's ownership (row-locked) and decide whether finalization stops.

    None: still ours; the lock stays held in the open transaction for the caller.
    ALREADY_TERMINAL: the job is cancelled/failed/done; the caller runs its cleanup.
    LOST_OWNERSHIP: another attempt holds the job; rolled back, nothing to release.
    """
    decision = finalize_exit(attempt_state(db, job_id, boot_user_id, attempt_token))
    if decision is None:
        return None
    db.rollback()
    if decision == "terminalized":
        _logger.info("Job %s: terminal at %s; not billing", job_id, where)
        return FinalizeOutcome(FinalizeKind.ALREADY_TERMINAL)
    _logger.info(
        "Job %s: the attempt token changed at %s; this attempt stops without billing, "
        "completing or releasing anything", job_id, where,
    )
    return FinalizeOutcome(FinalizeKind.LOST_OWNERSHIP)


def _settle_user_charge(db, job_id: str, billable_count: int, _billed_at):
    """Charge the user the delta between what was delivered and what the plan cap
    reserved, in the entitlement window the charge belongs to. The statement moved
    here verbatim from finalize_billing_and_done (see the comments above its call).
    Returns the RETURNING row, or None when the user counter did not move, which the
    caller treats as a billing failure."""
    return db.execute(
        sa_text(
            "WITH cur AS ("
            "  SELECT u.id, u.records_used, u.records_limit,"
            "         u.quota_anchor_at, u.quota_period_start,"
            "         u.quota_period_end, u.subscription_status,"
            "         u.entitlement_grace_ends_at, u.entitlement_ends_at,"
            "         u.pending_plan, u.pending_records_limit,"
            "         u.records_period_start,"
            "         j.quota_period_start AS job_window,"
            "         j.reserved_at AS job_reserved_at,"
            "         j.reserved_count AS job_reserved"
            "  FROM users u JOIN jobs j ON j.user_id = u.id"
            "  WHERE j.id = :jid FOR UPDATE OF u"
            "), w AS ("
            "  SELECT cur.*, " + window_cte_sql("", ":billed_at") + " FROM cur"
            "), s AS ("
            "  SELECT w.*, CASE WHEN "
            + reservation_is_current_sql(
                job_window="job_window",
                job_reserved_at="job_reserved_at",
                user_window="quota_period_start",
                user_records_period_start="records_period_start",
            )
            + "    THEN job_reserved ELSE 0 END AS applied_reserved"
            "  FROM w"
            ") UPDATE users u SET"
            "    records_used = GREATEST(0, s.base + (:billable - s.applied_reserved)),"
            + window_set_sql("s")
            + "  FROM s WHERE u.id = s.id"
            "  RETURNING s.applied_reserved, s.job_reserved, s.rolling"
        ),
        {"jid": job_id, "billable": billable_count,
         "billed_at": _billed_at},
    ).fetchone()


def _terminal_cleanup(db, job_id: str, boot_user_id) -> None:
    """A job found terminal during finalization delivered nothing: hand back its quota
    reservation and its dedup claims. Both re-check their own guards (the reservation
    only while unbilled and still in its window; claims only for a cancelled, unbilled
    job), so this is safe from every terminal exit and at most once in effect. Called
    after the finalization transaction was rolled back."""
    from src.workers.tasks_helpers.status import release_quota_reservation

    release_quota_reservation(db, job_id)
    _release_claims_of_cancelled_job(db, job_id, boot_user_id)
