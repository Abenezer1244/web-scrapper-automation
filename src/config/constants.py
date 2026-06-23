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


class NotificationType(str, Enum):
    """In-app notification kinds (Phase 2b). Mirrors the notification_prefs
    allowlist keys so one preference toggle governs both email and in-app."""

    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    PAYMENT_FAILED = "payment_failed"


class BatchRunStatus(str, Enum):
    """Batch-run state machine: pending -> running -> done | partial | failed
    | cancelled (`partial` = a mix of succeeded + failed children). Typing the
    response fields with this enum makes the OpenAPI schema emit the union, so
    the frontend's generated types carry it instead of a bare string."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"
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
# "phoneburner" = native PhoneBurner connector (per-contact outbox transport).
REGISTERED_DIALER_VENDOR_IDS: frozenset[str] = frozenset({"generic_webhook", "phoneburner"})
DEFAULT_DIALER_VENDOR_ID = "generic_webhook"

# Plans allowed to opt into the metered per-job `skip_trace_enabled`
# add-on ($0.08/lookup). Available to Pro and above.
SKIP_TRACE_ADDON_PLANS: frozenset[str] = frozenset({
    Plan.PRO.value,
    Plan.BUSINESS.value,
    Plan.AGENCY.value,
})

# Piece 2 (batch scrape). A batch fans out into many PAID scrapes, so it is
# naturally quota-bounded; gating is Pro+ (a productivity feature), not
# Business+ (Codex consult). Free/Starter stay single-scrape only.
BATCH_PLANS: frozenset[str] = frozenset({
    Plan.PRO.value,
    Plan.BUSINESS.value,
    Plan.AGENCY.value,
})

# Max (counties x record_types) combinations per batch, by plan — caps the
# cost/DoS blast radius. A remaining-monthly-quota preflight is a SEPARATE check.
BATCH_MAX_COMBINATIONS: dict[str, int] = {
    Plan.PRO.value: 25,
    Plan.BUSINESS.value: 100,
    Plan.AGENCY.value: 250,
}
# Absolute backstop regardless of plan (hard API-validation ceiling).
BATCH_HARD_CEILING: int = 250


# ── Value-metric entitlement matrix (docs/pricing-strategy-2026-06.md) ───────
# County access + record-type gating per tier. Defined here as the single source
# of truth; ENFORCED by src/api/entitlements.py ONLY when
# settings.ENTITLEMENT_ENFORCEMENT is true. Until that flag flips, the validator
# runs in audit/log-only mode (see that module's docstring for why).

# Per-plan cap on the number of DISTINCT counties a user may scrape across their
# active scraper configs. Count-based (any N counties, user's choice), -1 =
# unlimited. Matches the strategy's Starter 1 / Pro 3 / Business 10 / Agency all.
COUNTY_LIMIT_BY_PLAN: dict[str, int] = {
    Plan.STARTER.value: 1,
    Plan.PRO.value: 3,
    Plan.BUSINESS.value: 10,
    Plan.AGENCY.value: -1,
}

# The LIVE record-type slugs as stored in county_connectors.record_types — NOT
# the CLAUDE.md wishlist ("eviction"/"death_cert"), which have no live connector.
# Keep in sync with the registry; gating an unavailable type is meaningless.
ALL_RECORD_TYPES: frozenset[str] = frozenset({
    "probate",
    "pre_foreclosure",
    "tax_delinquent",
    "code_violation",
    "divorce",
    "death_certificate",
})

# Per-plan allowed record types. Starter = probate sample; Pro = the three
# broadly-available "core" distress lists; Business/Agency = every live type.
RECORD_TYPES_BY_PLAN: dict[str, frozenset[str]] = {
    Plan.STARTER.value: frozenset({"probate"}),
    Plan.PRO.value: frozenset({"probate", "pre_foreclosure", "tax_delinquent"}),
    Plan.BUSINESS.value: ALL_RECORD_TYPES,
    Plan.AGENCY.value: ALL_RECORD_TYPES,
}


# Export formats DataExporter.export() can actually produce — the single source
# of truth shared by the DeliverConfig save-time validator (reject bad NEW
# saves), the exporter dispatch (the runtime switch), and the worker (coerce a
# legacy/bad persisted value to a safe default instead of failing every scrape).
# "xlsx" is the on-disk alias of "excel"; both map to the same writer.
SUPPORTED_EXPORT_FORMATS: frozenset[str] = frozenset({"csv", "json", "excel", "xlsx"})
DEFAULT_EXPORT_FORMAT = "csv"


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


# DNC/TCPA compliance disclaimer surfaced to users on lead exports. It lives in
# the delivery email body + download UI — NOT inside the machine-import CSV/Excel
# (a disclaimer row corrupts a dialer import). Placement is not compliance: the
# real obligation is DNC-registry scrubbing (<=31 days) + records, done by the
# user's dialer/process. Kept as one constant so every surface shows identical copy.
DNC_DISCLAIMER: str = (
    "IMPORTANT: Verify numbers against the National DNC Registry and your state "
    "DNC list before calling. BridgeLeads does not currently pre-scrub phone "
    "numbers against DNC or TCPA litigator lists. Contacting a number on the DNC "
    "Registry without prior express consent may result in statutory damages of "
    "$500-$1,500 per call under the TCPA."
)
