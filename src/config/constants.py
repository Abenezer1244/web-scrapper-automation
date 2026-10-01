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

# ─── Job liveness thresholds ─────────────────────────────────────────────────
# A running job beats jobs.last_heartbeat_at every ~60s from run_scrape_job's
# HeartbeatThread. These two numbers decide when silence means "the worker is
# gone" — and they are read by BOTH the watchdog that re-queues the job
# (src/workers/scheduler_helpers/health.py) and the API that describes the job
# to the user (JobResponse in src/api/schemas.py). They live here so those two
# can never disagree: a UI that calls a job stalled earlier than the watchdog
# acts would alarm users about jobs nothing is about to fix, and one that calls
# it stalled later would keep claiming live progress after the re-queue.
# health.py's docstring already referred to "HEARTBEAT_STALE_MINUTES" as though
# it were a constant; it was a bare literal until now.
#
# 15 min: a live worker beats every ~60s, so this is 15 consecutive misses.
# Comfortably above the longest bounded blocking unit inside a beat interval
# (30s GIS chunk, 240s assessor cap) so a slow-but-alive step cannot trip it.
HEARTBEAT_STALE_MINUTES: int = 15

# run_scrape_job's Celery time limits. The hard one is also a PROOF, not just a
# timeout: the prefork pool kills the child that long after the task started, and
# the claim stamps started_at after the task starts, so no process can still be
# running an attempt more than RUN_SCRAPE_TIME_LIMIT_S after its started_at.
# Job.holds_run_slot leans on that to release a cancelled run whose worker died
# without acknowledging (audit #4 S4-02). The task decorator reads these, so the
# two cannot drift apart.
RUN_SCRAPE_SOFT_TIME_LIMIT_S: int = 3600  # 60 min: scrape + enrichment in one job
RUN_SCRAPE_TIME_LIMIT_S: int = 3900       # 65 min

# Fallback for rows with NO heartbeat observation at all (claimed before the
# heartbeat shipped / by an older worker image). Deliberately ABOVE the 65-min
# Celery hard time limit so a genuinely long, genuinely live job is never
# declared stuck on age alone. NULL is "unobserved", not "dead".
STUCK_STARTED_AT_FALLBACK_MINUTES: int = 70

# A job that was claimed but never stamped a started_at is a ZOMBIE: the worker
# died between the broker delivery and the claim. It has neither of the two
# signals above, so age since CREATION is the only evidence there is. A healthy
# job goes pending -> queued -> probing within seconds, so 10 minutes is already
# generous. The watchdog has always used this cutoff; the API needs it too, or a
# zombie reports "live" forever while the watchdog is re-queueing it.
# Both readers now take the number from here (Codex: the watchdog branch was
# still a bare literal after the API was hoisted onto the constant).
ZOMBIE_UNSTARTED_MINUTES: int = 10


# ─── Job stages (migration 099) ───────────────────────────────────────────────
# `status` is the state machine the watchdog, the billing CAS and the cancel
# endpoint all arbitrate on, so it stays coarse and must not grow. `stage` is the
# separate, purely descriptive answer to "what is the worker doing RIGHT NOW",
# which `status` cannot give: `scraping` covered 401 silent seconds of the one run
# we traced, and `enriching` covers saving, deduping, exporting, address lookup and
# contact queueing all at once.
#
# These are REAL boundaries in run_scrape_job, not a designed pipeline. They do
# NOT run in a fixed order and they REPEAT: the CSV export runs before enrichment
# and the scrapers do their own parcel lookup mid-scrape. Nothing may infer
# "step N of M" from this tuple.
#
# There is deliberately no `delivering`: email and webhook dispatch happen AFTER
# the job is `done` and after the terminal SSE event, so a stage write there would
# be refused by the terminal guard and would have no stream left to reach.
#
# Free text at the DB (like `trigger`) so adding one is a code change, not a
# migration on a hot table. This tuple is what the app actually writes, and
# src/api/schemas.py keys its user-facing copy off it — keep them in step.
JOB_STAGES: tuple[str, ...] = (
    "preparing",         # config, connector and entitlement resolution
    "connecting",        # browser launch, disclaimer, captcha: the long silent one
    "searching",         # query submitted, nothing counted back yet
    "scraping",          # pulling result units, counters moving
    "saving",            # persisting scraped rows
    "deduping",          # duplicate check against prior deliveries
    "exporting",         # building + uploading the CSV
    "enriching",         # property and mailing address lookup
    "queuing_contacts",  # skip-trace ENQUEUE only; the provider answers later
    "finalizing",        # billing settle + terminal transition
)

# What one unit of `units_done` / `units_total` IS. The UI renders this word, so a
# scraper that pages must not report its parcels as "pages" (Pierce did). NULL
# unit = unknown work shape; the UI then stays indeterminate rather than guessing.
JOB_PROGRESS_UNITS: tuple[str, ...] = ("page", "chunk", "parcel", "record")


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


# ── Per-plan export / schedule / segment access ──────────────────────────────
# The other half of the value-metric matrix. These three are what the plan cards
# sell as "CSV export" / "CSV + Excel export" / "All export formats",
# "Manual runs" / "Daily/weekly schedule" / "All schedules", and
# "All record types + overlap/intersection".
#
# All three shipped ungated: DeliverConfig validated `formats` against
# SUPPORTED_EXPORT_FORMATS alone, ScheduleConfig only checked that a frequency
# was a known word, and the /segments router carried no plan dependency at all.
# A Starter account could save a JSON export and a daily schedule, and reach the
# overlap lists sold on Business, by calling the API directly. Enforced from
# src/api/entitlements.py, at create and at edit, on the ENABLE-DELTA so a
# downgrade does not break a config its owner is only renaming.
#
# Unlike the county and record-type gates these are NOT behind
# ENTITLEMENT_ENFORCEMENT: that flag exists to stage the value-metric rollout for
# accounts that pre-date it, and it is true in production anyway.

# "xlsx" is the on-disk alias of "excel"; a plan that gets one gets the other, or
# the same file would be allowed under one name and refused under the other.
EXPORT_FORMATS_BY_PLAN: dict[str, frozenset[str]] = {
    Plan.STARTER.value: frozenset({"csv"}),
    Plan.PRO.value: frozenset({"csv", "excel", "xlsx"}),
    Plan.BUSINESS.value: SUPPORTED_EXPORT_FORMATS,
    Plan.AGENCY.value: SUPPORTED_EXPORT_FORMATS,
}

ALL_SCHEDULE_FREQUENCIES: frozenset[str] = frozenset(
    {"manual", "daily", "weekly", "monthly"}
)

# "manual" is in every set: it is the absence of a schedule, not a schedule, and
# refusing it would mean a Starter could not save a scraper at all.
SCHEDULE_FREQUENCIES_BY_PLAN: dict[str, frozenset[str]] = {
    Plan.STARTER.value: frozenset({"manual"}),
    Plan.PRO.value: frozenset({"manual", "daily", "weekly"}),
    Plan.BUSINESS.value: ALL_SCHEDULE_FREQUENCIES,
    Plan.AGENCY.value: ALL_SCHEDULE_FREQUENCIES,
}

# Overlap / intersection lead lists (/segments/*, and the batch
# delivery_mode="overlaps_only" export). Business and above, per the plan cards
# and docs/pricing-strategy-2026-06.md, which calls the distress-list overlap the
# crown jewel and gates it here deliberately.
OVERLAP_PLANS: frozenset[str] = frozenset({Plan.BUSINESS.value, Plan.AGENCY.value})

# Customer-facing names for export formats. "xlsx" and "excel" are one format
# with two spellings and must never be shown as two.
EXPORT_FORMAT_LABELS: dict[str, str] = {
    "csv": "CSV",
    "excel": "Excel",
    "xlsx": "Excel",
    "json": "JSON",
}


def export_format_label(fmt: str) -> str:
    """Customer-facing name for an export format slug."""
    key = (fmt or "").strip().lower()
    return EXPORT_FORMAT_LABELS.get(key) or key.upper()


def allowed_export_formats(plan: str) -> frozenset[str]:
    """Formats this plan may select. Fails CLOSED on an unknown plan."""
    return EXPORT_FORMATS_BY_PLAN.get(
        normalize_plan(plan), EXPORT_FORMATS_BY_PLAN[Plan.STARTER.value]
    )


def allowed_schedule_frequencies(plan: str) -> frozenset[str]:
    """Frequencies this plan may select. Fails CLOSED on an unknown plan."""
    return SCHEDULE_FREQUENCIES_BY_PLAN.get(
        normalize_plan(plan), SCHEDULE_FREQUENCIES_BY_PLAN[Plan.STARTER.value]
    )


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
    # Terminal, set by the retention sweep: this row's aged contact data has been
    # deleted (Privacy Policy §7). It establishes neither the lookup outcome nor a
    # charge. Intended for a former hit, but the sweep's _ELIGIBLE
    # (scheduler_helpers/retention.py) takes any aged row with a non-NULL contact
    # column: a MISS with [] arrays, or an ERRORED / unknown row holding older data.
    # That erases the MISS history this comment means to keep (owner decision, 2e
    # BUILD_JOURNAL 2026-10-01). Distinct from MISS
    # (we asked and got nothing) because the difference is auditable history, and
    # distinct from HIT so analytics stop counting it as enriched and the
    # enqueue path does not treat it as still-contactable. Deliberately NOT in
    # the ordinary re-enqueue predicate: retracing a purged row is a new PAID
    # vendor lookup and must be an explicit act, never a maintenance rerun.
    PURGED = "purged"


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


# How long after a Notice of Trustee Sale is RECORDED before it can first appear in
# a newspaper, and therefore in the nts_notices cache.
#
# RCW 61.24.040(1) records the notice of sale at least 90 days (120 with a 61.24.031
# letter) before the sale, and 61.24.040(5) publishes it between the 35th-28th and the
# 14th-7th day before the sale. First publication therefore lands no sooner than about
# 55 days after recording. Below this age a blank Auction Date is not evidence the
# source lacks the notice - the notice cannot legally exist in print yet.
#
# Measured against prod 2026-09-19: King leads recorded in Jul/Aug/Sep 2026 were 0/845
# matched, entirely explained by this window.
#
# One constant because two places ask the same question and must not drift: the
# matcher (src/workers/nts_matcher_task.auction_missing_reason) when it records WHY a
# lead is blank, and the results API when it reports a run's auction coverage.
AUCTION_PUBLICATION_LAG_DAYS: int = 55
