"""Shared domain constants — single source of truth for state-machine and feature-gate values.

Imported from API routes, workers, and schemas. When a new status, plan, or
config-mode value is added, update this file and every consumer picks it up.
Previously these values lived as ad-hoc set/tuple literals in each file,
which led to drift (e.g. `_CANCELLABLE_STATUSES` in jobs.py disagreed with
`active_statuses` in scheduler.py over whether `"probing"` was a real
state — jobs in that state could not be cancelled but also could not be
re-enqueued).
"""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    """Job state machine. PENDING -> QUEUED -> PROBING -> SCRAPING ->
    ENRICHING -> DONE | FAILED | CANCELLED.
    """

    PENDING = "pending"
    QUEUED = "queued"
    PROBING = "probing"
    SCRAPING = "scraping"
    ENRICHING = "enriching"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Statuses where `POST /jobs/{id}/cancel` is allowed. Includes PROBING —
# previously omitted, which left jobs stuck in that state without a way
# to cancel them while still blocking re-enqueue. PENDING is included
# because the worker has not picked the job up yet; QUEUED and the active
# states are included because the worker checks `job.status` periodically
# and bails out cleanly when it observes "cancelled".
CANCELLABLE_STATUSES: frozenset[str] = frozenset({
    JobStatus.PENDING.value,
    JobStatus.QUEUED.value,
    JobStatus.PROBING.value,
    JobStatus.SCRAPING.value,
    JobStatus.ENRICHING.value,
})

# Statuses that count as "the user already has an active job for this
# config" — used by the scheduler dispatcher to avoid double-enqueueing.
# Identical to CANCELLABLE_STATUSES today; kept as a separate name
# because the semantics are different and may diverge later.
ACTIVE_STATUSES: frozenset[str] = CANCELLABLE_STATUSES

# Statuses the watchdog re-queues if they have been sitting too long.
# Excludes PENDING because pending jobs may legitimately be waiting for
# capacity — only "stuck after worker pickup" cases should be re-queued.
STUCK_CHECK_STATUSES: frozenset[str] = frozenset({
    JobStatus.QUEUED.value,
    JobStatus.PROBING.value,
    JobStatus.SCRAPING.value,
    JobStatus.ENRICHING.value,
})


class Plan(str, Enum):
    """Subscription tiers."""

    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"
    AGENCY = "agency"


# Plans that get the high-priority Celery queue. Business+ paid tiers.
PRIORITY_QUEUE_PLANS: frozenset[str] = frozenset({Plan.BUSINESS.value, Plan.AGENCY.value})

# Plans allowed to use the per-config webhook delivery feature and the
# `enrichment.skip_tracing` toggle (the always-on enrichment, included
# with the plan).
BUSINESS_FEATURES_PLANS: frozenset[str] = frozenset({Plan.BUSINESS.value, Plan.AGENCY.value})

# Registered dialer-push connector ids (the `deliver.dialer_type` discriminator).
# Lives here (not in src.workers) so the API schema layer can validate the value
# WITHOUT importing the Celery app. A connector module must be registered AND its
# id listed here; the dialer_connectors registry asserts the two stay in sync.
# "generic_webhook" = the shipped vendor-agnostic webhook/Zapier push.
REGISTERED_DIALER_VENDOR_IDS: frozenset[str] = frozenset({"generic_webhook"})
DEFAULT_DIALER_VENDOR_ID = "generic_webhook"

# Plans allowed to opt into the metered per-job `skip_trace_enabled`
# add-on ($0.08/lookup). Available to Pro and above.
SKIP_TRACE_ADDON_PLANS: frozenset[str] = frozenset({
    Plan.PRO.value,
    Plan.BUSINESS.value,
    Plan.AGENCY.value,
})


class ScraperFrequency(str, Enum):
    """`schedule.frequency` values in ScraperConfig."""

    MANUAL = "manual"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class DateRangeMode(str, Enum):
    """`schedule.date_range_mode` values in ScraperConfig."""

    ROLLING_90 = "rolling_90"
    CUSTOM = "custom"
    SINCE_LAST_RUN = "since_last_run"


class SkipTraceStatus(str, Enum):
    """`Result.skip_trace_status` values."""

    NOT_ATTEMPTED = "not_attempted"
    QUEUED = "queued"
    SUBMITTED = "submitted"
    HIT = "hit"
    MISS = "miss"
    ERRORED = "errored"
