"""Body logic for the health beat tasks: watchdog_stuck_jobs + canary_check."""

from datetime import UTC, datetime, timedelta

from src.config.constants import STUCK_CHECK_STATUSES
from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

# Cap how many stuck jobs one watchdog tick re-delivers. Without a bound, a large
# orphan burst (the prod incident was 105) or a sustained worker backlog would
# re-`delay()` EVERY matching row on EVERY 5-min tick — the CAS dedupes execution,
# but the broker queue depth / Redis memory are not deduped, so unbounded
# re-delivery is an amplification vector. 500 drains the known incident in one
# tick with headroom; anything beyond is picked up (oldest-first) over the next
# few cycles since the rows persist until claimed.
_WATCHDOG_REDELIVER_LIMIT = 500


def _watchdog_stuck_jobs_impl() -> None:
    """Re-queue jobs that have been stuck in an ACTIVE state past the task budget.

    Runs every 5 minutes. Re-queues the job for retry up to max_retries times.

    Liveness is driven by jobs.last_heartbeat_at (migration 061): run_scrape_job
    beats every ~60s from a daemon thread while it owns the job. An active job is
    "stuck" only once that signal goes STALE (HEARTBEAT_STALE_MINUTES) — i.e. the
    worker is genuinely gone — regardless of how long the job has legitimately run.
    This replaces the old started_at-age cutoff, which fired on a LIVE long job (a
    24,708-parcel King tax enrich runs well past 20min): the watchdog re-queued a
    live job, and since run_scrape_job appended results on re-run that DOUBLED them
    (the 2026-06-17 duplication incident).

    NULL last_heartbeat_at = a pre-deploy job (started before this code shipped) or
    one that hasn't beat yet. For those we fall back to the conservative started_at
    cutoff (> the 65min Celery hard limit) so a live long job that predates the
    heartbeat is never falsely re-queued during the rolling deploy. New jobs beat
    within ~60s, so the heartbeat path governs them almost immediately.
    """
    from sqlalchemy import and_, or_, select

    from src.db.models import Job
    from src.db.session import system_sync_session
    from src.workers.tasks import run_scrape_job

    now = datetime.now(UTC)
    # A live worker beats every ~60s; 15min of silence = the worker is genuinely
    # gone (process hard-killed by Celery's time_limit, OOM, crash, broker loss).
    # Comfortably above the longest single bounded blocking unit (30s GIS chunk,
    # 240s assessor cap) so a slow-but-alive step can't trip it.
    heartbeat_cutoff = now - timedelta(minutes=15)
    # Fallback for NULL-heartbeat jobs only (pre-deploy / not-yet-beat): keep the
    # conservative > Celery-hard-limit (65min) cutoff so a LIVE long job is never
    # declared stuck while still running.
    stuck_cutoff = now - timedelta(minutes=70)
    # A job stuck in 'queued' state with started_at=NULL is a zombie
    # — the worker died before it could mark the job started. The
    # old predicate `Job.started_at < stuck_cutoff` returned NULL
    # for those rows and Postgres filtered them OUT, so they were
    # invisible to the watchdog forever. H8 from the full-SaaS
    # review: catch them via a separate "queued forever" branch.
    queued_cutoff = now - timedelta(minutes=10)

    with system_sync_session() as db:
        stuck_jobs = db.execute(
            select(Job).where(
                or_(
                    and_(
                        Job.status.in_(STUCK_CHECK_STATUSES),
                        or_(
                            # Heartbeat present and STALE → worker genuinely gone.
                            # A live worker beats every ~60s, so a healthy long
                            # job never matches this no matter its total runtime.
                            Job.last_heartbeat_at < heartbeat_cutoff,
                            # No heartbeat yet (pre-deploy / not-yet-beat): fall
                            # back to the conservative started_at cutoff (>65min
                            # Celery hard limit) so a live long job is never
                            # falsely re-queued. NULL started_at is the zombie
                            # case below, not this one.
                            and_(
                                Job.last_heartbeat_at.is_(None),
                                Job.started_at < stuck_cutoff,
                            ),
                            # Zombie: never got a started_at, but was created
                            # more than 10 minutes ago. A legitimate job goes
                            # from pending → queued → probing within seconds.
                            (Job.started_at.is_(None))
                            & (Job.created_at < queued_cutoff),
                        ),
                    ),
                    # Stranded retry (Codex P2): a job a PRIOR watchdog cycle reset
                    # to 'pending' whose re-enqueue failed (commit-before-delay +
                    # broker hiccup). Fresh pending jobs (retry_count 0) are still
                    # excluded — they may legitimately wait for capacity — but a
                    # watchdog RETRY (retry_count > 0) sitting 'pending' too long was
                    # likely stranded; re-pick it. Safe to re-enqueue: run_scrape_job's
                    # atomic pending->queued claim dedupes if one is actually in flight.
                    and_(
                        Job.status == "pending",
                        Job.retry_count > 0,
                        Job.started_at.is_(None),
                        Job.created_at < stuck_cutoff,
                    ),
                    # Orphaned fresh pending (enqueue-before-commit race): a job
                    # whose Celery message was consumed before its row committed
                    # 'pending' got rowcount=0 on the worker's atomic claim and
                    # was stranded — OR a post-fix broker publish that failed
                    # after commit-then-enqueue. retry_count==0 fresh pending is
                    # normally EXCLUDED (it may legitimately wait for capacity),
                    # but one created > 10 min ago that never got a started_at was
                    # never claimed. Re-deliver it; the worker's atomic
                    # pending->queued CAS dedupes if a worker is about to pick it
                    # up. This is the backstop for the 105-orphan prod incident.
                    and_(
                        Job.status == "pending",
                        Job.retry_count == 0,
                        Job.started_at.is_(None),
                        Job.created_at < queued_cutoff,
                    ),
                )
            )
            .order_by(Job.created_at.asc())
            .limit(_WATCHDOG_REDELIVER_LIMIT)
        ).scalars().all()

        requeued_ids: list[str] = []
        # M6: alert payloads queued here, dispatched only AFTER the commit below.
        pending_alerts: list[tuple[str, str, str, str]] = []
        for job in stuck_jobs:
            # A stranded retry (already 'pending' with retry_count>0, never started):
            # a PRIOR cycle already counted this retry; its enqueue just didn't land.
            # Re-deliver it WITHOUT bumping retry_count or failing on count — else,
            # under broker/worker backlog where created_at stays old, every tick
            # would burn a retry and fail the job before it ever ran (Codex P2). The
            # atomic claim dedupes if a worker is actually about to pick it up.
            if job.status == "pending" and job.retry_count > 0 and job.started_at is None:
                requeued_ids.append(job.id)
                _logger.warning(
                    "Watchdog: re-enqueueing stranded retry job %s (attempt %d/3)",
                    job.id, job.retry_count,
                )
                continue
            # Orphaned fresh pending (enqueue-before-commit race / failed publish):
            # re-deliver WITHOUT bumping retry_count — no scrape attempt was ever
            # made, so this is delivery repair, not a retry. The atomic claim
            # dedupes if a worker is concurrently picking it up.
            if job.status == "pending" and job.retry_count == 0 and job.started_at is None:
                requeued_ids.append(job.id)
                _logger.warning(
                    "Watchdog: re-delivering orphaned pending job %s "
                    "(enqueue-before-commit race; no retry burned)",
                    job.id,
                )
                continue
            if job.retry_count < 3:
                stuck_minutes = (
                    int((datetime.now(UTC) - job.started_at).total_seconds() / 60)
                    if job.started_at else "?"
                )
                job.retry_count += 1
                job.status = "pending"
                job.started_at = None
                # M12 (full-SaaS review): also reset the progress
                # counters so the UI doesn't show nonsense like
                # "Page 3 of 5" after a job was re-queued from
                # page 3. The retried scrape starts over from page 1.
                job.page_current = 0
                job.page_total = 0
                job.record_count = 0
                requeued_ids.append(job.id)
                _logger.warning(
                    "Watchdog: re-queued stuck job %s (attempt %d/3, stuck for %s min)",
                    job.id,
                    job.retry_count,
                    stuck_minutes,
                )
            else:
                job.status = "failed"
                job.finished_at = datetime.now(UTC)
                job.error_message = (
                    "This scraper run did not complete in time. "
                    "Our team has been notified and will investigate."
                )
                _logger.error("Watchdog: permanently failed job %s after 3 retries", job.id)
                # M6: queue the ops alert; sent AFTER commit (Codex P2 — an
                # alert for state that then fails to commit would also burn
                # the cooldown and suppress the later real alert).
                pending_alerts.append((
                    "watchdog",
                    str(job.id),
                    f"Job permanently failed after 3 retries ({job.id})",
                    f"job_id={job.id}\nuser_id={job.user_id}\n"
                    f"config_id={job.scraper_config_id}",
                ))

        db.commit()

    # Enqueue AFTER the commit (commit-before-delay, like dispatch_scheduled_jobs
    # and the batch fan-out): run_scrape_job now claims pending->queued with an
    # atomic CAS, so a worker that consumes the retry before the 'pending' reset
    # is committed would read the stale active status, get rowcount 0, and bail —
    # stranding the job 'pending' with no message. Committing first guarantees the
    # worker sees 'pending' and can claim it (Codex P2). A per-job try/except keeps
    # one broker failure from aborting the rest; anything that fails here stays
    # committed 'pending' with retry_count>0 and is re-picked by the next cycle's
    # stranded-retry branch above.
    for jid in requeued_ids:
        try:
            run_scrape_job.delay(jid)
        except Exception:  # noqa: BLE001 — committed 'pending'; re-picked next cycle
            _logger.warning(
                "watchdog: re-enqueue of %s failed (will re-pick next cycle)",
                jid, exc_info=True,
            )

    # M6: dispatch alerts AFTER the commit (Codex P2) — never for state that
    # didn't durably land, and never burning a cooldown on a rolled-back fail.
    from src.workers.ops_alerts import send_ops_alert
    for kind, key, subject, body in pending_alerts:
        send_ops_alert(kind, key, subject, body)


def _canary_check_impl() -> None:
    """Run a 1-page test scrape per active connector to verify portal health.

    Updates county_connectors.health_status:
      - 'healthy'  — canary returned ≥ 1 record
      - 'degraded' — canary returned 0 records (portal reachable but empty)
      - 'down'     — canary threw an exception
    """
    import asyncio

    from sqlalchemy import select

    from src.api.middleware.security import register_connector_domains_from_db
    from src.db.models import CountyConnector
    from src.db.session import SyncSessionLocal
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class

    # Refresh the in-process SSRF allowlist so connectors added via
    # POST /scrapers/connectors after this worker booted are not falsely
    # marked `down` by the next canary cycle. Without this, the API
    # process's add_scrape_domain() call doesn't propagate to the worker
    # that runs canary_check, the canary's validate_scraping_target()
    # rejects the new base_url, and list_connectors() then hides the
    # connector from users (it filters out `down` rows by default).
    register_connector_domains_from_db()

    with SyncSessionLocal() as db:
        all_connectors = db.execute(
            select(CountyConnector).where(CountyConnector.active)
        ).scalars().all()

        # Check max 5 counties per run (rotate through all over time)
        import random
        connectors = random.sample(all_connectors, min(5, len(all_connectors)))
        _logger.info("Canary: checking %d/%d counties", len(connectors), len(all_connectors))

        # M6: alert payloads queued here, dispatched only AFTER the commit below.
        pending_alerts: list[tuple[str, str, str, str]] = []
        for connector in connectors:
            # M6: alert only on the TRANSITION into 'down' (not every tick a
            # connector stays down — the cooldown also dedupes, belt+suspenders).
            prev_status = connector.health_status
            try:
                scraper_class, _ = get_scraper_class(
                    connector.county, connector.state, connector.record_types[0]
                )
                # Probe a 7-day window. A 1-day window produces
                # false-positive "degraded" statuses for rural counties
                # that routinely file 0 probates or pre-foreclosures on
                # a given day. 7 days is short enough to stay cheap but
                # long enough that any county with meaningful filing
                # volume will return at least one record when healthy.
                today = datetime.now(UTC).date()
                week_ago = today - timedelta(days=7)

                records = asyncio.run(
                    _canary_scrape(scraper_class, week_ago.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y"))
                )
                # Sticky health: once a connector has been marked
                # 'healthy', do NOT downgrade it to 'degraded' just
                # because the most recent 7-day probe returned zero
                # records. Small counties oscillate based on which
                # week the canary happens to sample, and flipping the
                # status causes them to vanish from the user-facing
                # connectors endpoint. Only a real exception path
                # (caught below) downgrades a healthy connector.
                # Non-healthy connectors still get upgraded normally
                # when they produce records.
                if records:
                    connector.health_status = "healthy"
                elif connector.health_status != "healthy":
                    # Was degraded/down/unknown and is still empty —
                    # stay in whatever non-healthy state we had, or
                    # move to 'degraded' if we were 'unknown'.
                    if connector.health_status in ("unknown", "down"):
                        connector.health_status = "degraded"
                _logger.info(
                    "Canary %s/%s: %s (%d records)",
                    connector.county, connector.state, connector.health_status, len(records)
                )
            except UnsupportedCountyError:
                _logger.warning("Canary: no scraper for %s/%s", connector.county, connector.state)
                connector.health_status = "down"
            except Exception as exc:
                _logger.error("Canary failed for %s/%s: %s", connector.county, connector.state, exc)
                connector.health_status = "down"
                # M6: a previously-working portal just broke — queue an ops page
                # (was log-only). Sent post-commit; exception CLASS only in the
                # email (scraper errors can embed raw page content = PII risk —
                # Codex P2; the full error is already in worker logs above).
                if prev_status != "down":
                    pending_alerts.append((
                        "canary",
                        f"{connector.county}/{connector.state}",
                        f"Canary DOWN: {connector.county}/{connector.state} "
                        f"(was {prev_status})",
                        f"connector={connector.county}/{connector.state}\n"
                        f"previous_status={prev_status}\n"
                        f"error_class={type(exc).__name__} (full error in worker logs)",
                    ))

            connector.last_checked = datetime.now(UTC)

        db.commit()

    # M6: alerts only after the health statuses durably committed (Codex P2).
    from src.workers.ops_alerts import send_ops_alert
    for kind, key, subject, body in pending_alerts:
        send_ops_alert(kind, key, subject, body)


async def _canary_scrape(scraper_class, date_from: str, date_to: str) -> list:
    async with scraper_class() as scraper:
        return await scraper.scrape(date_from, date_to)
