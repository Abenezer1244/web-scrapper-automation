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

from src.config import settings
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
    _reconcile_quota_periods_impl,
    _reset_skip_trace_usage_impl,
)
from src.workers.scheduler_helpers.contact_lookups import _reconcile_contact_lookups_impl
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
    _enrichment_source_canary_impl,
    _watchdog_stuck_jobs_impl,
)
from src.workers.scheduler_helpers.meter import _flush_skip_trace_meter_outbox_impl
from src.workers.scheduler_helpers.onboarding import _send_onboarding_emails_impl
from src.workers.scheduler_helpers.public_cache import _refresh_public_sample_cache_impl
from src.workers.scheduler_helpers.registration import (
    _dispatch_pending_verification_emails_impl,
    _purge_expired_pending_registrations_impl,
)
from src.workers.scheduler_helpers.retention import _purge_skip_trace_pii_impl

_logger = setup_logger("worker.scheduler")

# ─── Beat schedule ────────────────────────────────────────────────────────────
#
# Anything every 10 minutes or slower is a wall-clock crontab, not an interval.
# Beat's schedule file is not on a volume, so every deploy starts every entry
# fresh, and a fresh interval entry waits one FULL period before its first run.
# On 2026-09-15 (UTC) beat containers started at 00:59, 01:12, 01:20 and 01:29,
# and the 20-minute recover-deferred-property sweep never fired once. A crontab fires at
# the next mark after boot however often deploys land. Beat runs in UTC
# (src/workers/__init__.py). Entries under 10 minutes stay intervals: a deploy
# costs them at most one short period, and aligning them to shared marks would
# only bunch the paid and dispatch sweeps together.
#
# The three King recovery sweeps share one eRealProperty lease, so their marks
# are staggered at least 2 minutes apart: mailing :03 +10, owners :05 +15,
# property :07 +20. tests/test_beat_schedule.py pins both rules.

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
    "sweep-stranded-quota-reservations": {
        "task": "src.workers.scheduler.sweep_quota_reservations",
        "schedule": 300.0,  # every 5 minutes, alongside the watchdog
    },
    "sweep-stranded-dedup-claims": {
        "task": "src.workers.scheduler.sweep_dedup_claims",
        "schedule": 300.0,  # every 5 minutes, alongside the reservation sweep
    },
    "canary-check": {
        "task": "src.workers.scheduler.canary_check",
        "schedule": crontab(minute=17),  # hourly at :17
    },
    "db-latency-canary": {
        # One fresh connection + SELECT 1 on the API's path; an ops alert after 3
        # bad probes in a row (src/workers/db_canary.py). On 2026-10-01 the
        # database was slow for ~6 hours before login broke, and nothing watched.
        # expires: a probe that sat in the queue past the next tick measures the
        # queue, not the database, so it is dropped rather than run late.
        "task": "src.workers.db_canary.db_latency_canary",
        "schedule": 120.0,  # every 2 minutes
        "options": {"expires": 110},
    },
    "enrichment-source-canary": {
        # The recovery half of external_source_health, which shipped without one:
        # probe each blocked enrichment source whose cooldown has expired and
        # either clear it or escalate. Until this existed, a blocked source could
        # only leave cooldown by passive expiry, and the next real job then spent
        # a full circuit-breaker window rediscovering the block and re-armed it.
        # Verified in production: king_erealproperty sat throttled for three days
        # with last_probe_at NULL while the source answered 60/60 with HTTP 200.
        #
        # Every 5 minutes, but the cost is NOT one probe per 5 minutes: a healthy
        # source has no row and is never probed, and a source still inside its
        # cooldown is not due. The claim also enforces a 10-minute floor between
        # probes of the same source. So this is a handful of requests per OUTAGE,
        # not per tick, and it keeps recovery latency under the shortest ladder rung.
        "task": "src.workers.scheduler.enrichment_source_canary",
        "schedule": 300.0,  # every 5 minutes
    },
    "reconcile-quota-periods": {
        # Record quota is metered over each user's own ENTITLEMENT WINDOW, not
        # the calendar month (migration 088). Correctness does NOT live here:
        # reserve and settle advance the window atomically in the statement that
        # charges. This is the safety net for users those paths never touch, and
        # the repair for a Stripe webhook that never arrived — so it runs hourly
        # rather than daily, and being late costs at most an hour of a stale
        # /billing/usage reading, never a wrong charge.
        "task": "src.workers.scheduler.reconcile_quota_periods",
        "schedule": crontab(minute=41),  # hourly at :41
    },
    "reset-skip-trace-usage": {
        # Formerly "reset-monthly-usage". The RECORDS half of that task was
        # retired when entitlement windows landed: running it alongside anchored
        # windows would zero a 20th-anchored subscriber twice, once on their own
        # boundary and again on the 1st. What remains is skip-trace, which is
        # billed on its own Stripe meter against its own period column and is
        # still calendar-metered by deliberate decision.
        #
        # Still DAILY rather than a cron on the 1st: Celery Beat does not
        # backfill missed ticks, so a redeploy at that instant would skip a
        # whole month. Idempotent — it only touches users whose
        # skip_trace_period_start is earlier than the current month.
        "task": "src.workers.scheduler.reset_skip_trace_usage",
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
        "schedule": crontab(minute=25),  # hourly at :25
    },
    "purge-skip-trace-pii": {
        # Privacy Policy §7 retention: clear aged vendor-sourced contact PII off
        # `results` and delete aged `skip_trace_cache` rows. DAILY, not weekly:
        # a weekly sweep would leave PII in place for up to ~7 days past the
        # retention boundary we publish, and "365 days" should not quietly mean
        # 372. Runs behind RETENTION_PURGE_ENABLED / _DRY_RUN.
        "task": "src.workers.scheduler.purge_skip_trace_pii",
        "schedule": crontab(hour=4, minute=10),  # daily, off-peak, after the 3am weekly
    },
    "purge-expired-pending-registrations": {
        # Email-verification flow: delete pending_registrations rows whose verify
        # window lapsed so the table can't grow from abandoned/sprayed signups.
        "task": "src.workers.scheduler.purge_expired_pending_registrations",
        "schedule": crontab(minute=39),  # hourly at :39
    },
    "dispatch-pending-verification-emails": {
        # Email-verification OUTBOX: send the verification email for each due
        # pending_registrations row and record the outcome on the row. The row is
        # the durable record, so a signup made while Redis (the broker) was down
        # is drained and sent here once Redis recovers — never lost on a
        # fire-and-forget enqueue from the request path.
        "task": "src.workers.scheduler.dispatch_pending_verification_emails",
        "schedule": 60.0,  # every 60s — verification emails should feel prompt
    },
    "onboarding-emails": {
        "task": "src.workers.scheduler.send_onboarding_emails",
        "schedule": crontab(hour=14, minute=0),  # 2 PM UTC = 7 AM PT
    },
    "dispatch-pending-skip-trace": {
        # Sprint 4: drains pending_skip_trace_rows, submits Tracerfy batches.
        # Tracerfy rate-limits batch POSTs to 10 per 5 min, so we run every
        # SKIP_TRACE_DISPATCH_INTERVAL_SECONDS (default 300) and submit at most
        # SKIP_TRACE_MAX_BATCHES_PER_TICK (default 2) per tick. The pause-state
        # heartbeat reads the same setting. No-op if SKIP_TRACE_ENABLED=False.
        "task": "src.workers.skip_trace_dispatcher.dispatch_pending_skip_trace",
        "schedule": float(settings.SKIP_TRACE_DISPATCH_INTERVAL_SECONDS),
    },
    "flush-skip-trace-meter-outbox": {
        # REDTEAM (Codex convergence — meter outbox): recover skip-trace
        # MeterEvents whose inline post-commit enqueue was lost (broker down /
        # worker crash). Sweeps skip_trace_meter_events rows still
        # reported_at IS NULL and re-enqueues report_skip_trace_meter_event.
        "task": "src.workers.scheduler.flush_skip_trace_meter_outbox",
        "schedule": 180.0,  # every 3 minutes
    },
    "reconcile-contact-lookups": {
        # Phase 1b-2c: expire, take back, re-publish and settle "look up contacts"
        # actions. Never bills. Under 10 minutes, so a plain interval is allowed.
        "task": "src.workers.scheduler.reconcile_contact_lookups",
        "schedule": 120.0,  # every 2 minutes
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
        "schedule": crontab(minute=57),  # hourly at :57
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
        # "Legals" PDF (Pacific Publishing).
        # DAILY, not weekly (2026-09-03). The old Thursday-only schedule was a
        # single-point-of-failure: the legals page exposes ONLY the current issue and
        # there is no archive link, so ONE missed or failed Thursday lost that week's
        # notices PERMANENTLY — nothing ever revisited them. Measured on King, which
        # ran the identical schedule: only 4 of 14 published issues were ever ingested.
        # The current issue stays up all week, so a daily run turns "miss a week" into
        # "miss a day, recover tomorrow"; a real outage now needs 7 consecutive
        # failures, which _alert_if_crawl_barren pages on. Re-fetching the same PDF is
        # cheap (~250 KB) and the (source, ts_number) upsert is idempotent, so extra
        # runs only refresh fetched_at — which also stops the 90-day _CACHE_DAYS sweep
        # from expiring a still-live notice. Barren alerts are keyed per source behind
        # a 6h cooldown, so daily cannot turn one page into seven.
        "task": "src.workers.nts_crawler.crawl_nts_snoho_tribune",
        "schedule": crontab(hour=10, minute=45),  # 10:45 UTC daily
    },
    "crawl-nts-king-queenanne": {
        # NTS Tier 1 (King, PARTIAL coverage): the Queen Anne & Magnolia News weekly
        # "Legals" PDF. Daily for the same reason as Snohomish above — this is the
        # schedule that demonstrably lost 10 of 14 issues. King's dominant venue is
        # still the DJC (paid, deferred), so this remains supplemental King coverage.
        "task": "src.workers.nts_crawler.crawl_nts_king_queenanne",
        "schedule": crontab(hour=10, minute=50),  # 10:50 UTC daily
    },
    "crawl-nts-columbian-clark": {
        # NTS Tier 1 (Clark): The Columbian classifieds publishes Clark County trustee
        # sales as a single rolling HTML listing. Daily like Tacoma (the listing updates
        # continuously, not weekly); runs after the AM scrape + the matcher picks it up.
        "task": "src.workers.nts_crawler.crawl_nts_columbian_clark",
        "schedule": crontab(hour=10, minute=35, day_of_week="*"),  # 10:35 UTC daily
    },
    "recover-deferred-mailing": {
        # The recovery half of `mailing_lookup_deferred`, which shipped as a
        # marker that nothing ever read: parcels a source outage skipped were
        # deferred with a comment promising "a later sweep can find them", and no
        # sweep existed. Bounded, gated on source health, mailing-only. It never
        # bills, never creates a job and never enqueues a skip trace.
        #
        # Every 10 minutes, but a tick with no deferred King rows is a single
        # indexed query and exits, and a tick while King is in cooldown makes no
        # request at all.
        "task": "src.workers.mailing_recovery.recover_deferred_mailing",
        "schedule": crontab(minute="3-59/10"),  # every 10 minutes from :03
    },
    "data-quality-sweep": {
        # Judges each job that finished in the last day, once, against its county x
        # record-type baseline (parcel / property / mailing / phone / email coverage,
        # mailing-echoes-property). A collapse raises an ops alert; nothing about the
        # job changes. Hourly is plenty: the signal is "this county went dark", not
        # "this row is late". See src/workers/data_quality.py.
        "task": "src.workers.data_quality.data_quality_sweep",
        "schedule": crontab(minute=33),  # hourly at :33, clear of the King sweeps
    },
    "recover-deferred-owners": {
        # The reading half of `owner_lookup_deferred`: names delivered King tax
        # leads whose owner lookup a job could not finish. Bounded (120 parcels,
        # 300 s), gated on source health and OWNER_RECOVERY_ENABLED, shares the
        # King source lease with every other eRealProperty pass. Never bills,
        # never creates a job, never enqueues a skip trace.
        "task": "src.workers.owner_recovery.recover_deferred_owners",
        "schedule": crontab(minute="5-59/15"),  # every 15 minutes from :05
    },
    "recover-code-violation-owners": {
        # Names delivered King code-violation leads the job's 240 s owner pass did
        # not reach: Seattle SDCI leads located on a shown parcel, and Bellevue,
        # Burien and King County Accela leads by the parcel_id they printed. Bounded (120 parcels, 300 s), gated on source health and
        # OWNER_RECOVERY_ENABLED, shares the King source lease with every other
        # eRealProperty pass. Never bills, never creates a job, never enqueues a
        # skip trace. At least 2 minutes from every other King sweep's start
        # (tests/test_beat_schedule.py).
        "task": "src.workers.cv_owner_recovery.recover_code_violation_owners_task",
        "schedule": crontab(minute="18-59/20"),  # :18, :38, :58 every hour
    },
    "recover-deferred-property": {
        # The reading half of `property_lookup_deferred`: fills King property
        # addresses a job could not look up (condo unit extract first, then the
        # eRealProperty page under the shared King lease). Only leads a job marked,
        # delivered only; bounded (120 parcels, 300 s), gated on source health and
        # PROPERTY_RECOVERY_ENABLED. Never bills, never creates a job, never
        # enqueues a skip trace, never copies mailing into property.
        "task": "src.workers.property_recovery.recover_deferred_property",
        "schedule": crontab(minute="7-59/20"),  # every 20 minutes from :07
    },
    "recover-pierce-cv-owners": {
        # Names delivered Tacoma code-violation leads the job's bounded owner pass did
        # not reach (Pierce ATIP taxpayer record; owner decision 2026-09-14 scopes it to
        # code violations). Bounded (20 parcels, 300 s), PIERCE_CV_OWNER_ENABLED
        # (default off), its own Pierce lease and source cooldown; NOT on the King
        # lease, so it is not one of the King sweeps. Never bills, never creates a
        # job, never enqueues a skip trace. Minutes :10/:40 sit at least 2 min from
        # every King mark.
        "task": "src.workers.pierce_cv_owner_recovery.recover_pierce_cv_owners_task",
        "schedule": crontab(minute="10-59/30"),  # every 30 minutes from :10
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

@app.task(name="src.workers.scheduler.sweep_quota_reservations")
def sweep_quota_reservations() -> int:
    """Return quota held by terminal jobs that never billed.

    The plan cap charges a reservation up front so concurrent jobs cannot be
    allocated the same allowance. A job that ends without billing is therefore
    holding records the user never received. The in-task release paths cover
    normal failures; this catches whatever terminalized a job WITHOUT running
    them — an external cancel, a batch force-finalize, the watchdog's permanent
    fail — so a stranded grant can never become a silent permanent charge.
    """
    from src.workers.tasks_helpers.status import sweep_stranded_quota_reservations

    return sweep_stranded_quota_reservations()


@app.task(name="src.workers.scheduler.sweep_dedup_claims")
def sweep_dedup_claims() -> int:
    """Release dedup claims held by failed or cancelled jobs that never billed.

    The companion of sweep_quota_reservations for the other thing a job holds
    before it delivers. Without it, a job that ended while no worker was running
    it keeps its claims forever, and every later run hides those leads as
    "already delivered" although nothing was delivered or charged.
    """
    from src.workers.tasks_helpers.status import sweep_stranded_dedup_claims

    return sweep_stranded_dedup_claims()


@app.task(name="src.workers.scheduler.watchdog_stuck_jobs")
def watchdog_stuck_jobs() -> None:
    """Fail jobs that have been stuck in an active state for > 55 minutes.

    Runs every 5 minutes. Re-queues the job for retry up to max_retries times.
    EagleWeb chunked scraping can take 15-20min scrape + 15min DB save = 35min.
    """
    return _watchdog_stuck_jobs_impl()


# ─── Task 3: Canary health checks ────────────────────────────────────────────

@app.task(name="src.workers.scheduler.enrichment_source_canary")
def enrichment_source_canary() -> None:
    """Probe blocked ENRICHMENT SOURCES whose cooldown expired; clear or escalate.

    Sibling of canary_check, deliberately separate: that one asks "can we still
    scrape this county portal" (county_connectors), this one asks "can we still
    enrich from this external source" (external_source_health). Different tables,
    different failure modes, and conflating them would mean a portal outage could
    clear an enrichment block or vice versa.
    """
    return _enrichment_source_canary_impl()


@app.task(name="src.workers.scheduler.canary_check")
def canary_check() -> None:
    """Run a 1-page test scrape per active connector to verify portal health.

    Updates county_connectors.health_status:
      - 'healthy'  — canary returned ≥ 1 record
      - 'degraded' — canary returned 0 records (portal reachable but empty)
      - 'down'     — canary threw an exception
    """
    return _canary_check_impl()


# ─── Task 4: Entitlement-window reconciliation + skip-trace reset ────────────

@app.task(name="src.workers.scheduler.reconcile_quota_periods")
def reconcile_quota_periods() -> dict[str, int]:
    """Advance record-quota entitlement windows that have ended.

    Record quota resets on each user's own entitlement anniversary, not on the
    1st (migration 088). The authoritative rollover happens LAZILY, inside the
    atomic statement that reserves or settles a job's quota — so a user who is
    actively scraping is always metered against the right window without this
    task existing at all.

    This is the reconciliation for everyone else: users who transact rarely,
    plus the repair for a Stripe ``customer.subscription.deleted`` that never
    arrived. It is idempotent, tolerates missed runs, skips users a worker is
    mid-charge on (SKIP LOCKED) rather than blocking them, and can never grant a
    duplicate bucket — a user gone for three months gets ONE window, not three.

    Hourly, because being late costs at most a stale ``/billing/usage`` reading
    and never a wrong charge.
    """
    return _reconcile_quota_periods_impl()


@app.task(name="src.workers.scheduler.reset_skip_trace_usage")
def reset_skip_trace_usage() -> None:
    """Roll over the SKIP-TRACE counter on the calendar month.

    Formerly ``reset_monthly_usage``, which also reset record quota. That half
    is retired: record quota now follows the entitlement window, and running
    both would zero a 20th-anchored subscriber on their own boundary AND again
    on the 1st. Skip-trace is billed on its own Stripe meter against its own
    ``skip_trace_period_start`` column and stays calendar-metered deliberately.

    H5 (full-SaaS review): runs DAILY at 00:05 UTC rather than on a cron at the
    1st. Celery Beat does not backfill missed ticks, so if Beat was down at that
    instant (Railway redeploy, broker hiccup) the reset was skipped entirely and
    every user carried last month's usage into the new month. Idempotent: a user
    already rolled this month has skip_trace_period_start = this month and is
    skipped.
    """
    return _reset_skip_trace_usage_impl()


# ─── Task 5: Expire free trials ──────────────────────────────────────────────

@app.task(name="src.workers.scheduler.expire_trials")
def expire_trials() -> None:
    """Downgrade expired trial users from Pro to Starter.

    Runs hourly. Finds users where trial_ends_at < now and plan is still 'pro'
    with no stripe_customer_id (paying users keep their plan).
    """
    return _expire_trials_impl()


@app.task(name="src.workers.scheduler.purge_expired_pending_registrations")
def purge_expired_pending_registrations() -> None:
    """Delete expired pending_registrations rows (email-verification flow).

    Hourly. Removes rows whose verify window lapsed so the table can't grow
    unbounded from abandoned or sprayed signups.
    """
    return _purge_expired_pending_registrations_impl()


@app.task(name="src.workers.scheduler.purge_skip_trace_pii")
def purge_skip_trace_pii() -> None:
    """Purge aged skip-trace PII (Privacy Policy §7 retention).

    Daily. NULLs vendor-sourced contact fields on `results` rows past the
    retention window and deletes aged `skip_trace_cache` rows, keeping the lead
    row itself (county public record) intact. No-ops unless
    RETENTION_PURGE_ENABLED; logs without writing while RETENTION_PURGE_DRY_RUN.
    """
    return _purge_skip_trace_pii_impl()


@app.task(name="src.workers.scheduler.dispatch_pending_verification_emails")
def dispatch_pending_verification_emails() -> None:
    """Send due verification emails from the pending_registrations outbox.

    Every 60s. Sends each due row's verification email and records the outcome on
    the row, so a signup made while Redis (the broker) was down is sent once it
    recovers. No-op when EMAIL_VERIFICATION_ENABLED is off.
    """
    return _dispatch_pending_verification_emails_impl()


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


# ─── Task: Reconcile contact-lookup actions (every 2 min) ────────────────────

@app.task(name="src.workers.scheduler.reconcile_contact_lookups")
def reconcile_contact_lookups() -> dict:
    """Expire, take back, re-publish and settle contact-lookup actions (Phase 1b-2c).

    See src/workers/scheduler_helpers/contact_lookups.py. It never bills.
    """
    return _reconcile_contact_lookups_impl()


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
