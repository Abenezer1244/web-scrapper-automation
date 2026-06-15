"""Celery beat scheduler: periodic task REGISTRATION + beat schedule.

The 15 @app.task definitions below stay here unchanged (decorator, name= string,
signature, queue=) — moving a registration out of this module would silently
break the prod beat schedule. Each task BODY is delegated to a thin helper in
src/workers/scheduler_helpers/ (grouped by theme); the task is the wrapper.

Re-exports (kept importable from src.workers.scheduler for callers/tests):
  _should_run_now, _dispatch_due_batches, _materialize_dialer_outbox,
  _canary_scrape, BATCH_LEASE_MINUTES, BATCH_FORCE_MINUTES,
  BATCH_PENDING_REDISPATCH_MINUTES.
"""

from celery.schedules import crontab

from src.utils.logger import setup_logger
from src.workers import app

# ─── Re-exports (preserve the historical scheduler.py import surface) ────────
from src.workers.scheduler_helpers.batch import (  # noqa: F401
    BATCH_FORCE_MINUTES,
    BATCH_LEASE_MINUTES,
    BATCH_PENDING_REDISPATCH_MINUTES,
    _batch_completion_sweep_impl,
    _batch_recovery_sweep_impl,
)
from src.workers.scheduler_helpers.billing import (
    _expire_trials_impl,
    _reset_monthly_usage_impl,
)
from src.workers.scheduler_helpers.county import (
    _purge_old_records_impl,
    _run_single_county_scrape_impl,
    _scrape_county_daily_impl,
)
from src.workers.scheduler_helpers.dialer import (  # noqa: F401
    _dialer_push_sweep_impl,
    _materialize_dialer_outbox,
)
from src.workers.scheduler_helpers.dispatch import (  # noqa: F401
    _dispatch_due_batches,
    _dispatch_scheduled_batches_impl,
    _dispatch_scheduled_jobs_impl,
    _should_run_now,
)
from src.workers.scheduler_helpers.health import (  # noqa: F401
    _canary_check_impl,
    _canary_scrape,
    _watchdog_stuck_jobs_impl,
)
from src.workers.scheduler_helpers.meter import _flush_skip_trace_meter_outbox_impl
from src.workers.scheduler_helpers.onboarding import _send_onboarding_emails_impl
from src.workers.scheduler_helpers.public_cache import _refresh_public_sample_cache_impl

_logger = setup_logger("worker.scheduler")

# ─── Beat schedule ────────────────────────────────────────────────────────────

app.conf.beat_schedule = {
    "dispatch-scheduled-jobs": {
        "task": "src.workers.scheduler.dispatch_scheduled_jobs",
        "schedule": 60.0,  # every 1 minute
    },
    "dispatch-scheduled-batches": {
        # 2B: recurring batch scrapes — creates a 'pending' BatchRun when a
        # batch's schedule fires; Track A dispatch/recovery does the rest.
        "task": "src.workers.scheduler.dispatch_scheduled_batches",
        "schedule": 60.0,  # every 1 minute
    },
    "watchdog-stuck-jobs": {
        "task": "src.workers.scheduler.watchdog_stuck_jobs",
        "schedule": 300.0,  # every 5 minutes
    },
    "canary-check": {
        "task": "src.workers.scheduler.canary_check",
        "schedule": 3600.0,  # every 1 hour
    },
    "reset-monthly-usage": {
        # H5 (full-SaaS review): daily catch-up instead of cron-on-1st.
        # The task is idempotent — it only resets users whose
        # records_period_start is earlier than the current month, so
        # running it daily has the same net effect when Beat is
        # healthy and catches up cleanly when Beat was down at the
        # instant of the 1st-of-month rollover.
        "task": "src.workers.scheduler.reset_monthly_usage",
        "schedule": crontab(hour=0, minute=5),  # 00:05 UTC daily
    },
    "scrape-county-daily": {
        "task": "src.workers.scheduler.scrape_county_daily",
        "schedule": crontab(hour=9, minute=0),  # 9 AM UTC = 2 AM PT (Seattle)
    },
    "purge-old-records": {
        "task": "src.workers.scheduler.purge_old_records",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),
    },
    "expire-trials": {
        "task": "src.workers.scheduler.expire_trials",
        "schedule": 3600.0,  # every 1 hour
    },
    "onboarding-emails": {
        "task": "src.workers.scheduler.send_onboarding_emails",
        "schedule": crontab(hour=14, minute=0),  # 2 PM UTC = 7 AM PT
    },
    "dispatch-pending-skip-trace": {
        # Sprint 4: drains pending_skip_trace_rows, submits Tracerfy batches.
        # Tracerfy rate-limits batch POSTs to 10 per 5 min, so we run every
        # 5 min and submit at most SKIP_TRACE_MAX_BATCHES_PER_TICK (default 2)
        # per tick. The task is a no-op if SKIP_TRACE_ENABLED=False.
        "task": "src.workers.skip_trace_dispatcher.dispatch_pending_skip_trace",
        "schedule": 300.0,  # every 5 minutes
    },
    "flush-skip-trace-meter-outbox": {
        # REDTEAM (Codex convergence — meter outbox): recover skip-trace
        # MeterEvents whose inline post-commit enqueue was lost (broker down /
        # worker crash). Sweeps skip_trace_meter_events rows still
        # reported_at IS NULL and re-enqueues report_skip_trace_meter_event.
        "task": "src.workers.scheduler.flush_skip_trace_meter_outbox",
        "schedule": 180.0,  # every 3 minutes
    },
    "dialer-push-sweep": {
        # Phase 5: push dialer-ready leads for jobs whose async skip-trace has
        # SETTLED (can't push at scrape completion — cache-miss phones arrive
        # later via the Tracerfy webhook). Claims each job once via
        # Job.dialer_pushed_at. No-op when no config has a dialer_webhook_url.
        "task": "src.workers.scheduler.dialer_push_sweep",
        "schedule": 300.0,  # every 5 minutes
    },
    "refresh-public-sample-cache": {
        # RLS cutover Phase 2b: precompute the sanitized landing-page samples
        # so the public /scrapers/sample endpoint reads a cache row instead of
        # live-querying tenant tables (results/jobs/scraper_configs). Hourly is
        # plenty — the landing page tolerates stale-by-an-hour sample rows.
        "task": "src.workers.scheduler.refresh_public_sample_cache",
        "schedule": 3600.0,  # every 1 hour
    },
    "crawl-nts-tacoma-index": {
        # NTS Tier 1: harvest Pierce trustee-sale auction data (auction date /
        # default amount / trustee) from the Tacoma Daily Index legal notices into
        # the nts_notices cache. Daily is plenty — WA NTS publish 7-35 days before
        # the sale (RCW 61.24.040), so the data isn't same-day perishable.
        "task": "src.workers.nts_crawler.crawl_nts_tacoma_index",
        "schedule": crontab(hour=10, minute=30),  # 10:30 UTC daily (after the AM scrape)
    },
    "match-nts-notices": {
        # NTS Tier 1: attach freshly-crawled auction data onto recent unmatched
        # Pierce pre_foreclosure leads. Runs after the crawl so the cache is warm.
        "task": "src.workers.nts_matcher_task.match_nts_notices",
        "schedule": crontab(hour=11, minute=0),  # 11:00 UTC daily (30m after crawl)
    },
    "crawl-nts-snoho-tribune": {
        # NTS Tier 1 (Snohomish): the Snohomish County Tribune publishes a weekly
        # "Legals" PDF (Pacific Publishing). Weekly cadence — the paper prints once a
        # week (Wednesdays), so a daily crawl would just re-fetch the same PDF. Runs
        # Thursdays so the new issue is up; the matcher's daily run attaches it.
        "task": "src.workers.nts_crawler.crawl_nts_snoho_tribune",
        "schedule": crontab(hour=10, minute=45, day_of_week=4),  # Thu 10:45 UTC
    },
    "crawl-nts-king-queenanne": {
        # NTS Tier 1 (King, PARTIAL coverage): the Queen Anne & Magnolia News weekly
        # "Legals" PDF. Same weekly cadence; runs Thursdays. King's dominant venue is
        # the DJC (paid, deferred), so this is supplemental King NTS coverage.
        "task": "src.workers.nts_crawler.crawl_nts_king_queenanne",
        "schedule": crontab(hour=10, minute=50, day_of_week=4),  # Thu 10:50 UTC
    },
    "batch-completion-sweep": {
        # Piece 2: finalize batch_runs whose child jobs are ALL terminal — build
        # the one combined CSV + deliver. Claims each run via a reclaimable lease;
        # force-finalizes a run stuck past the hard deadline (Track A).
        "task": "src.workers.scheduler.batch_completion_sweep",
        "schedule": 60.0,  # every 1 minute
    },
    "batch-recovery-sweep": {
        # Track A: crash recovery for the dispatch windows — re-dispatch a
        # 'pending' run whose .delay() was lost, and re-enqueue 'pending' children
        # of a 'running' run (both bounded). Keeps a batch from stranding.
        "task": "src.workers.scheduler.batch_recovery_sweep",
        "schedule": 120.0,  # every 2 minutes
    },
}


# ─── Task 1: Dispatch scheduled jobs ─────────────────────────────────────────

@app.task(name="src.workers.scheduler.dispatch_scheduled_jobs")
def dispatch_scheduled_jobs() -> None:
    """Enqueue jobs for all active scraper configs whose schedule matches now.

    Runs every minute. Idempotent — checks for an existing pending/running job
    for the same config before enqueuing to prevent duplicates.
    """
    return _dispatch_scheduled_jobs_impl()


# ─── Task 1b: Dispatch scheduled BATCHES (2B) ────────────────────────────────

@app.task(name="src.workers.scheduler.dispatch_scheduled_batches")
def dispatch_scheduled_batches() -> None:
    """2B: enqueue a run for every active batch whose schedule matches now.

    Runs every minute (mirrors dispatch_scheduled_jobs). The created 'pending'
    run is the durable intent — if the .delay below is lost, batch_recovery_sweep
    re-dispatches it; everything downstream (fan-out, completion barrier,
    combined CSV, delivery) is the existing Track A machinery.
    """
    return _dispatch_scheduled_batches_impl()


# ─── Task 2: Watchdog for stuck jobs ─────────────────────────────────────────

@app.task(name="src.workers.scheduler.watchdog_stuck_jobs")
def watchdog_stuck_jobs() -> None:
    """Fail jobs that have been stuck in an active state for > 55 minutes.

    Runs every 5 minutes. Re-queues the job for retry up to max_retries times.
    EagleWeb chunked scraping can take 15-20min scrape + 15min DB save = 35min.
    """
    return _watchdog_stuck_jobs_impl()


# ─── Task 3: Canary health checks ────────────────────────────────────────────

@app.task(name="src.workers.scheduler.canary_check")
def canary_check() -> None:
    """Run a 1-page test scrape per active connector to verify portal health.

    Updates county_connectors.health_status:
      - 'healthy'  — canary returned ≥ 1 record
      - 'degraded' — canary returned 0 records (portal reachable but empty)
      - 'down'     — canary threw an exception
    """
    return _canary_check_impl()


# ─── Task 4: Monthly usage reset (daily catch-up) ────────────────────────────

@app.task(name="src.workers.scheduler.reset_monthly_usage")
def reset_monthly_usage() -> None:
    """Roll over monthly usage counters when the billing period ends.

    H5 (full-SaaS review): previously this ran on a crontab at
    day_of_month=1, hour=0, minute=0. Celery Beat does NOT backfill
    missed cron ticks — if Beat was down at that instant (Railway
    redeploy, broker hiccup) the reset was SKIPPED ENTIRELY and every
    user carried last month's records_used forward into the new
    month. A user at 500/500 last month started the new month
    instantly at cap.

    Now runs daily at 00:05 UTC. On each run, finds users whose
    records_period_start points at a month strictly earlier than
    this run's current month and resets their counters +
    advances records_period_start to the first of the current
    month. Idempotent: a user who was already reset this month
    has records_period_start = this month and is skipped.

    The same logic applies to skip_trace_used_this_month +
    skip_trace_period_start for Sprint 4 billing.
    """
    return _reset_monthly_usage_impl()


# ─── Task 5: Expire free trials ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.expire_trials")
def expire_trials() -> None:
    """Downgrade expired trial users from Pro to Starter.

    Runs hourly. Finds users where trial_ends_at < now and plan is still 'pro'
    with no stripe_customer_id (paying users keep their plan).
    """
    return _expire_trials_impl()


# ─── Task 6: Daily county scrape ────────────────────────────────────────────

@app.task(name="src.workers.scheduler.scrape_county_daily")
def scrape_county_daily() -> None:
    """Dispatch daily scrape for each active county. Runs at 2 AM UTC."""
    return _scrape_county_daily_impl(run_single_county_scrape)


@app.task(name="src.workers.scheduler.run_single_county_scrape", queue="scrape")
def run_single_county_scrape(county: str, state: str) -> None:
    """Scrape a single county's daily records into county_records cache."""
    return _run_single_county_scrape_impl(county, state)


# ─── Task 6: Purge old records ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.purge_old_records")
def purge_old_records() -> None:
    """Delete county_records older than RECORD_RETENTION_DAYS. Weekly."""
    return _purge_old_records_impl()


# ─── Task: Refresh public sample cache (landing page) ───────────────────────

@app.task(name="src.workers.scheduler.refresh_public_sample_cache")
def refresh_public_sample_cache() -> None:
    """Recompute the sanitized landing-page samples + stats into
    public_sample_cache (RLS cutover Phase 2b).

    The public /scrapers/sample endpoint reads ONLY this precomputed row, so an
    unauthenticated request never live-queries the tenant tables
    (results/jobs/scraper_configs) and the API role needs no cross-tenant read
    policy for it. ALL PII redaction happens HERE, so the cached payload is safe
    to serve publicly. Runs via system_sync_session (cross-tenant, no RLS user
    context) — under the cutover the bridgeleads_system FOR ALL policy applies.
    """
    return _refresh_public_sample_cache_impl()


# ─── Task: Onboarding emails (daily at 7 AM PT) ─────────────────────────────

@app.task(name="src.workers.scheduler.send_onboarding_emails")
def send_onboarding_emails() -> None:
    """Send day-1 nudge, day-3 activation reminder, day 6-7 trial expiry warnings."""
    return _send_onboarding_emails_impl()


# ─── Task: Flush skip-trace meter outbox (every 3 min) ──────────────────────

@app.task(name="src.workers.scheduler.flush_skip_trace_meter_outbox")
def flush_skip_trace_meter_outbox() -> None:
    """Recover skip-trace Stripe MeterEvents whose inline enqueue was lost.

    REDTEAM (Codex convergence — meter outbox): the Tracerfy ingest worker
    commits a skip_trace_meter_events outbox row per billable user in the same
    transaction that advances the usage counter, then best-effort enqueues
    report_skip_trace_meter_event for each. If the broker was down at that
    instant — or the worker crashed between commit and enqueue — the row sits
    with reported_at IS NULL and would never be billed. This sweep picks those
    up and re-enqueues them.

    Runs every ~3 minutes. Only sweeps rows older than 30 seconds so the inline
    enqueue gets first crack (avoids a duplicate enqueue racing the fast path;
    the report task is idempotent on reported_at anyway). The report task fires
    the Stripe MeterEvent with a stable (queue_id, user_id) identifier, so a
    re-enqueue can neither lose the event nor double-bill.
    """
    return _flush_skip_trace_meter_outbox_impl()


# ─── Task: Dialer push sweep (Phase 5) ───────────────────────────────────────

@app.task(name="src.workers.scheduler.dialer_push_sweep")
def dialer_push_sweep() -> None:
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
    return _dialer_push_sweep_impl()


# ─── Piece 2: batch completion barrier ──────────────────────────────────────

@app.task(name="src.workers.scheduler.batch_completion_sweep")
def batch_completion_sweep() -> None:
    """Finalize batch_runs whose child jobs are ALL terminal — build the one
    combined CSV + deliver. Does NOT wait on async skip-trace: the CSV is built on
    property identity (ready at child enrichment); contacts fill in later and the
    CSV is re-downloadable. No-op when no run is ready.

    Track A: the claim is a LEASE (claimed_at + claim_token) reclaimable after
    BATCH_LEASE_MINUTES, so a worker hard-killed mid-finalize can't strand a run
    'running' forever (Gap 2). A run still 'running' past BATCH_FORCE_MINUTES is
    FORCE-finalized even with a missing / stuck child, through this SAME claim +
    finalize path (Gap 3b) — one code path, eligibility differs.
    """
    return _batch_completion_sweep_impl()


@app.task(name="src.workers.scheduler.batch_recovery_sweep")
def batch_recovery_sweep() -> None:
    """Pre-finalize crash recovery for the batch dispatch windows (Track A):

      Gap 1  — a 'pending' run whose dispatch .delay() was lost: the batch sits
               with no jobs. Re-dispatch it (idempotent) every sweep. If it still
               hasn't materialized after BATCH_FORCE_MINUTES, give up and mark it
               'failed' so it can't sit 'pending' forever.
      Gap 3a — a 'running' run with children still 'pending' (a lost child .delay):
               re-enqueue them every sweep (the atomic claim dedupes in-flight).

    All enqueues happen AFTER commit (commit-before-delay). dispatch_batch_run is
    idempotent (FOR UPDATE + UNIQUE(batch_id)); run_scrape_job's atomic claim makes
    a re-enqueue of an in-flight child a no-op.
    """
    return _batch_recovery_sweep_impl()
