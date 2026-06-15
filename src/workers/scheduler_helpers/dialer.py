"""Body logic for the dialer_push_sweep beat task (Phase 5)."""

from datetime import UTC, datetime, timedelta

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _materialize_dialer_outbox(db, job, config, vendor_id: str, leads: list[dict]) -> None:
    """Idempotently INSERT one pending dialer_deliveries row per lead.

    ON CONFLICT DO NOTHING on UNIQUE(job_id, result_id) so a re-run sweep (or a
    crash-retry) never duplicates a contact. No credentials are stored — only the
    routing keys the outbox transport needs to re-read config + result at send time.
    """
    from uuid import uuid4

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.db.models import DialerDelivery

    if not leads:
        return
    rows = [
        {
            "id": str(uuid4()),
            "job_id": job.id,
            "result_id": ld["id"],
            "user_id": job.user_id,
            "scraper_config_id": config.id,
            "vendor_id": vendor_id,
            "status": "pending",
        }
        for ld in leads
    ]
    db.execute(
        pg_insert(DialerDelivery)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_dialer_delivery_job_result")
    )


def _dialer_push_sweep_impl() -> None:
    """Push dialer-ready leads for done jobs whose skip-trace has SETTLED.

    Deferred from scrape completion on purpose: skip-trace is async — cache-miss
    rows are marked queued/submitted and their phone/DNC are filled in later by
    the Tracerfy webhook, so a push at completion would miss exactly the leads
    we want (Codex). A job is "settled" when no Result of it is still
    queued/submitted. Each job is claimed once via Job.dialer_pushed_at, so even
    a job with zero dialer-ready leads is evaluated only once. Reuses
    deliver_job_webhook (SSRF re-validate, HMAC, retry, non-fatal). No-op when no
    config has a dialer_webhook_url.
    """
    from sqlalchemy import and_, func, or_, select, update

    from src.api.dialer_filters import dialer_ready_conditions
    from src.config.constants import BUSINESS_FEATURES_PLANS
    from src.db.models import Job, PendingSkipTraceRow, Result, ScraperConfig, User
    from src.db.session import system_sync_session
    from src.workers.dialer_connectors import get_connector
    from src.workers.dialer_outbox import process_dialer_outbox
    from src.workers.webhook_delivery import DIALER_PUSH_CAP, deliver_job_webhook

    _BATCH = 50
    # Jobs still waiting on async skip-trace. Base "settled" on the pending QUEUE
    # (the authoritative async-work tracker), NOT Result.skip_trace_status: when
    # Tracerfy submission errors, the dispatcher marks the pending row 'errored'
    # but leaves Result 'queued', so a Result-based check would NEVER settle that
    # job (Codex). queued/submitted = in-flight; completed/errored = terminal.
    #
    # Settlement nuance (Codex), queued vs submitted differ:
    #  - 'queued' rows are local dispatcher backlog — the dispatcher WILL process
    #    them even if it's down/backlogged for a while, so they ALWAYS block
    #    settlement until they leave the queue. Aging them out would claim the
    #    job before the phones exist, and it would never push again.
    #  - 'submitted' rows are in-flight at Tracerfy and CAN get stuck if the
    #    completed CSV omits them; those age out past the cutoff so a wedged row
    #    can't block the push forever. Age by submitted_at (COALESCE to
    #    enqueued_at only as a defensive fallback for a NULL submitted_at).
    _stale_cutoff = datetime.now(UTC) - timedelta(hours=12)
    _submitted_age = func.coalesce(
        PendingSkipTraceRow.submitted_at, PendingSkipTraceRow.enqueued_at
    )
    unsettled = (
        select(PendingSkipTraceRow.id)
        .where(
            PendingSkipTraceRow.job_id == Job.id,
            or_(
                PendingSkipTraceRow.status == "queued",
                and_(
                    PendingSkipTraceRow.status == "submitted",
                    _submitted_age >= _stale_cutoff,
                ),
            ),
        )
        .exists()
    )

    with system_sync_session() as db:
        candidates = db.execute(
            select(Job, ScraperConfig)
            # Owner-match in the join (Codex security, defense-in-depth): the DB
            # doesn't enforce job.user_id == config.user_id, and this sweep runs
            # in a system session that bypasses RLS — without this, a malformed
            # job (user A) pointing at user B's config could push A's lead PII to
            # B's dialer_webhook_url. The job-create path already enforces it; this
            # closes the gap if a bad row ever exists.
            .join(
                ScraperConfig,
                (ScraperConfig.id == Job.scraper_config_id)
                & (ScraperConfig.user_id == Job.user_id),
            )
            # Re-check entitlement at push time against the owner's CURRENT plan:
            # the dialer push is a Business+ feature, and a user who configured it
            # then downgraded must NOT keep pushing lead PII (Codex). Gating in SQL
            # also keeps downgraded users' jobs out of the candidate set entirely.
            .join(User, User.id == Job.user_id)
            .where(
                Job.status == "done",
                Job.dialer_pushed_at.is_(None),
                User.plan.in_(BUSINESS_FEATURES_PLANS),
                # A dialer-push candidate has EITHER a generic webhook URL OR a
                # native vendor dialer_type (e.g. phoneburner, which has no URL —
                # it pushes from the outbox). ->> returns NULL for a missing key.
                or_(
                    and_(
                        ScraperConfig.deliver.op("->>")("dialer_webhook_url").isnot(None),
                        ScraperConfig.deliver.op("->>")("dialer_webhook_url") != "",
                    ),
                    and_(
                        ScraperConfig.deliver.op("->>")("dialer_type").isnot(None),
                        ScraperConfig.deliver.op("->>")("dialer_type").notin_(
                            ("", "generic_webhook")
                        ),
                    ),
                ),
                ~unsettled,
            )
            .limit(_BATCH)
        ).all()

        pushed = 0
        for job, config in candidates:
            deliver = config.deliver or {}
            connector = get_connector(deliver.get("dialer_type"))
            url = deliver.get("dialer_webhook_url")
            # The generic webhook connector needs a destination URL; outbox vendors
            # (e.g. phoneburner) push from the dialer_deliveries outbox and have none.
            if not connector.uses_outbox and not url:
                continue  # defensive: SQL filter should already guarantee this
            # DURABLE CLAIM BEFORE PUBLISH (at-most-once): atomically stamp
            # dialer_pushed_at from NULL and COMMIT before enqueuing. This both
            # serializes concurrent sweeps (only one UPDATE flips NULL->now) and
            # closes the publish-before-commit window — if we enqueued first and
            # then crashed pre-commit, the next sweep would re-push (Codex). A
            # lost push after a successful claim is recoverable via manual export;
            # a duplicate dialer import is not, so claim-first is the safe order.
            claimed = db.execute(
                update(Job)
                .where(Job.id == job.id, Job.dialer_pushed_at.is_(None))
                .values(dialer_pushed_at=datetime.now(UTC))
            ).rowcount
            db.commit()
            if not claimed:
                continue  # another sweep claimed it first
            try:
                # NOT-known-DNC (include_unknown_dnc=True): skip-trace populates
                # phone but leaves phone_dnc_flag NULL (Tracerfy returns no DNC),
                # so a strict `IS FALSE` would match nothing and the feature would
                # push zero leads (Codex). The destination dialer performs the
                # authoritative DNC scrub; this excludes only KNOWN-DNC numbers
                # and is forward-safe if DNC data is added later.
                conds = dialer_ready_conditions(include_unknown_dnc=True)
                # Exclude cross-job duplicates: a recurring scrape re-finds an
                # already-delivered lead and stores it is_duplicate=true; pushing
                # it (with a NEW result id as external_id) would re-import the same
                # contact every run (Codex). Push only fresh leads.
                conds = [*conds, Result.is_duplicate.is_(False)]
                total = db.execute(
                    select(func.count()).select_from(Result).where(
                        Result.job_id == job.id, Result.user_id == job.user_id, *conds
                    )
                ).scalar_one()
                if total > 0:
                    rows = db.execute(
                        select(Result)
                        .where(Result.job_id == job.id, Result.user_id == job.user_id, *conds)
                        .order_by(Result.id)  # deterministic before LIMIT
                        .limit(DIALER_PUSH_CAP)
                    ).scalars().all()
                    leads = [
                        {
                            "id": row.id,
                            "party_name": row.party_name,
                            "phone": row.phone,
                            "phone_type": row.phone_type,
                            "phone_dnc_flag": row.phone_dnc_flag,
                            "email": row.email,
                            "property_address": row.property_address,
                            "mailing_address": row.mailing_address,
                        }
                        for row in rows
                    ]
                    # Dispatch via the dialer connector seam (Thread 3).
                    if connector.uses_outbox:
                        # Native bulk-less vendor: materialize one durable outbox row
                        # per lead (idempotent via UNIQUE(job_id,result_id)), then
                        # drain via process_dialer_outbox — durable per-contact state
                        # + replay. Credentials are NOT touched here; the transport
                        # re-reads them from the DB at send time.
                        _materialize_dialer_outbox(db, job, config, connector.VENDOR_ID, leads)
                        db.commit()
                        process_dialer_outbox.delay(str(job.id))
                    else:
                        # Generic webhook: builds the exact same payload + URL as
                        # before (locked by tests/test_dialer_connector_base.py) and
                        # enqueues one delivery — byte-identical to the prior path.
                        job_meta = {
                            "job_id": str(job.id),
                            "scraper_config_id": str(config.id),
                            "scraper_name": config.name,
                            "county": config.county,
                            "state": config.state,
                            "record_type": config.record_type,
                            "total_dialer_ready_count": total,
                            "dialer_webhook_url": url,
                            "dialer_webhook_secret": deliver.get("dialer_webhook_secret"),
                        }
                        for req in connector.build_requests(leads, job_meta):
                            deliver_job_webhook.delay(str(job.id), req["url"], req["payload"])
                    pushed += 1
                    _logger.info(
                        "Dialer push queued for job %s: %d of %d dialer-ready leads (%s)",
                        job.id, len(leads), total, connector.VENDOR_ID,
                    )
                # Already claimed+committed above. A 0-lead job stays claimed so
                # it isn't re-evaluated every sweep forever.
            except Exception as exc:  # noqa: BLE001 — one bad job must not stall the sweep
                # The enqueue is the LAST step in the try and only raises BEFORE
                # the message is sent, so reaching here means nothing was pushed.
                # Un-claim (dialer_pushed_at back to NULL) so a transient error
                # retries next sweep — without reopening the duplicate-push window
                # (a crash here leaves the claim set, which is the safe direction).
                # Never log the URL (token risk) or lead PII — message only.
                try:
                    db.rollback()
                    db.execute(
                        update(Job)
                        .where(Job.id == job.id)
                        .values(dialer_pushed_at=None)
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                _logger.warning(
                    "Dialer push sweep: job %s failed (will retry next sweep): %s",
                    job.id, str(exc)[:200],
                )

        if pushed:
            _logger.info("Dialer push sweep: enqueued %d job push(es)", pushed)
