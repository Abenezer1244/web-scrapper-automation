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


# Length of the free Pro trial granted at registration, in days. Single source
# of truth: the registration handler stamps trial_ends_at from this, and the
# welcome email quotes it. They previously each carried their own literal 7.
TRIAL_PERIOD_DAYS: int = 7


class Plan(str, Enum):
    """Subscription tiers."""

    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"
    AGENCY = "agency"


# Plans that get the high-priority Celery queue. Business+ paid tiers.
PRIORITY_QUEUE_PLANS: frozenset[str] = frozenset({Plan.BUSINESS.value, Plan.AGENCY.value})

# The queue names run_scrape_job can be published to. `scrape-priority` is
# consumed ahead of `scrape` in WORKER_QUEUES, which under the Redis broker's
# default round-robin strategy means a paid job never waits behind the whole
# free-tier backlog. It is NOT strict priority; kombu would need
# broker_transport_options={"queue_order_strategy": "priority"} for that.
SCRAPE_QUEUE_PRIORITY = "scrape-priority"
SCRAPE_QUEUE_DEFAULT = "scrape"


def normalize_plan(plan: str | None) -> str:
    """The canonical slug for a stored plan value.

    Every gate in this codebase keys off the lowercase catalog ids, but plans are
    not always written by the Stripe webhook: on this deployment they are also set
    by hand in the database, which is how a "Business" or a "pro " gets in. Half
    the call sites used to compare raw and the other half lowercased without
    stripping, so the same stored value could be refused webhook delivery, routed
    off the priority queue, and enforced as Starter, all silently and all in
    different directions. One helper, used everywhere, is the fix.

    Unknown values are returned as-is (lowercased and stripped) rather than
    coerced to a default: the gates already fail closed on an unrecognized plan,
    and silently renaming it here would hide the bad row instead of denying it.
    """
    return (plan or "starter").strip().lower()


def scrape_queue_for_plan(plan: str | None) -> str:
    """Which queue a scrape job for this plan is published to.

    Used by EVERY enqueue site. The scheduled dispatcher and the batch fan-out
    used to call ``run_scrape_job.delay()``, which takes the task's declared
    route (``scrape``) regardless of plan, so the Agency "Priority queue" line
    applied to a manual button press and to nothing else. Recurring runs are
    exactly the work the tier is bought for.
    """
    return (
        SCRAPE_QUEUE_PRIORITY
        if normalize_plan(plan) in PRIORITY_QUEUE_PLANS
        else SCRAPE_QUEUE_DEFAULT
    )

# Transient scrape-failure retry policy (Codex-reconciled). When a scrape phase
# raises a TransientScrapeError / Playwright infra error, the worker re-queues the
# job with escalating backoff instead of permanently failing the whole day's run
# on one flaky page. 2 retries = 3 total attempts. The Nth retry waits
# SCRAPE_TRANSIENT_BACKOFF_SECONDS[N-1] (last value reused if retries ever exceed
# the tuple length); a small random jitter is added at enqueue time to avoid a
# thundering herd when many jobs fail at once.
SCRAPE_TRANSIENT_MAX_RETRIES: int = 2
SCRAPE_TRANSIENT_BACKOFF_SECONDS: tuple[int, ...] = (300, 1200)  # 5 min, then 20 min

# Plans allowed to use the per-config webhook delivery feature, the dialer push,
# and the `enrichment.skip_tracing` toggle.
#
# NOTE on `enrichment.skip_tracing`: this comment used to call it "the always-on
# enrichment, included with the plan". It is not. The worker's skip-trace entry
# point reads the `skip_trace_enabled` COLUMN and never looks at the enrichment
# blob, so the toggle is gated, persisted, and read by nothing. It is kept gated
# (a Business+ field should stay a Business+ field) but the route now mirrors it
# onto `skip_trace_enabled`, which is the flag that actually runs a lookup.
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
    "trustee_sale",
})

# Per-plan allowed record types. Starter = probate sample; Pro = the core distress
# lists (incl. trustee_sale "Auction Leads"); Business/Agency = every live type.
# trustee_sale is added to PRO explicitly — Pro does NOT inherit ALL_RECORD_TYPES.
RECORD_TYPES_BY_PLAN: dict[str, frozenset[str]] = {
    Plan.STARTER.value: frozenset({"probate"}),
    Plan.PRO.value: frozenset(
        {"probate", "pre_foreclosure", "tax_delinquent", "trustee_sale"}
    ),
    Plan.BUSINESS.value: ALL_RECORD_TYPES,
    Plan.AGENCY.value: ALL_RECORD_TYPES,
}


# ─── Display labels ──────────────────────────────────────────────────────────
# Slugs are the storage/wire form; these are the ONLY forms a customer should
# ever read. Plan NAMES are not here: they live on the catalog entries in
# src/config/plans.py (`plan_label`), which is the single source of truth for
# anything a customer is shown about a plan.
#
# This is the one record-type label map. src/api/routes/segments.py and
# src/workers/batch_export.py both used to carry their own identical copy; the
# worker kept a local one only to avoid importing an API route module, which
# this module is not.
RECORD_TYPE_LABELS: dict[str, str] = {
    "probate": "Probate",
    "pre_foreclosure": "Pre-Foreclosure",
    "tax_delinquent": "Tax Delinquent",
    "code_violation": "Code Violation",
    "divorce": "Divorce",
    "death_certificate": "Death Certificate",
    "trustee_sale": "Trustee Sale",
    # No live connector yet, but it was in both of the maps this replaces and
    # the fallback would render it identically anyway.
    "eviction": "Eviction",
}


def record_type_label(slug: str) -> str:
    """Customer-facing name for a record-type slug. An unmapped slug degrades to
    a title-cased version of itself rather than leaking the raw underscore form."""
    key = (slug or "").strip().lower()
    return RECORD_TYPE_LABELS.get(key) or key.replace("_", " ").title()


def count_label(n: int, singular: str, plural: str) -> str:
    """"1 county" / "2 counties". English pluralization is not the caller's job to
    re-derive at every message site, and getting it wrong is the kind of detail a
    customer reads as sloppiness."""
    return f"{n} {singular if n == 1 else plural}"


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
