import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    api_key_hash = Column(String(64), nullable=True, index=True)
    plan = Column(String(32), nullable=False, default="starter")
    records_used = Column(Integer, nullable=False, default=0)
    records_limit = Column(Integer, nullable=False, default=50)
    # H5 (full-SaaS review): track the start of the current
    # billing period so the daily reset task can detect a missed
    # month-boundary reset (Celery Beat downtime on the 1st) and
    # catch up on the next run instead of silently carrying last
    # month's records_used forward.
    records_period_start = Column(DateTime(timezone=True), nullable=True)
    stripe_customer_id = Column(String(64), nullable=True)
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    # Sprint 4: skip-trace usage counter for bundled-quota + overage billing
    skip_trace_used_this_month = Column(Integer, nullable=False, default=0)
    skip_trace_period_start = Column(DateTime(timezone=True), nullable=True)
    # Sprint 7.3: referral program — each user has a unique shareable
    # code; referred_by_user_id is set when they sign up via another
    # user's link; referral_credit_cents accumulates $20 per successful
    # conversion of a referred user to a paid plan.
    referral_code = Column(String(16), unique=True, nullable=True, index=True)
    referred_by_user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    referral_credit_cents = Column(Integer, nullable=False, default=0)
    # Per-user email notification preferences (settings → Notifications).
    # Allowlisted boolean keys: job_completed, job_failed, new_records,
    # usage_alert, payment_failed. Empty dict = service defaults apply.
    notification_prefs = Column(JSON, nullable=False, default=dict, server_default="{}")
    is_active = Column(Boolean, nullable=False, default=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    # Logout-everywhere / password-reset / account-compromise revocation
    # timestamp. Any JWT issued at-or-before this moment is rejected by
    # get_current_user. Persisted in DB (not just Redis) so JWT
    # validation still has a durable source of truth when Upstash is
    # rate-limited or unreachable — see the Phase 0 Codex adversarial
    # review of the auth fail-closed posture.
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    # MFA (H2): TOTP. mfa_secret_encrypted holds a Fernet token of the TOTP
    # secret (src/utils/crypto.py) — never the raw secret. Backup codes live in
    # the mfa_backup_codes table. mfa_enabled gates the login MFA challenge.
    mfa_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    mfa_secret_encrypted = Column(Text, nullable=True)
    mfa_enrolled_at = Column(DateTime(timezone=True), nullable=True)
    # Highest TOTP 30s timestep counter already consumed at login (H2-P4). Login
    # advances it atomically so a code can't be replayed inside pyotp's ±1 window.
    mfa_last_totp_counter = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    scraper_configs = relationship("ScraperConfig", back_populates="user", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")


class PasswordHistory(Base):
    """Stores recent password hashes to prevent reuse."""
    __tablename__ = "password_history"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MfaBackupCode(Base):
    """One-time MFA backup codes (H2).

    Stored as keyed HMAC-SHA256 hex digests, never plaintext. Each code is
    consumed atomically (``used_at`` set under a row lock) so the same code
    can't be replayed or used concurrently. Codes are shown to the user exactly
    once, at enable time.
    """
    __tablename__ = "mfa_backup_codes"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False)  # HMAC-SHA256 hex digest
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScraperConfig(Base):
    __tablename__ = "scraper_configs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    county = Column(String(128), nullable=False)
    state = Column(String(2), nullable=False)
    record_type = Column(String(64), nullable=False)
    fields = Column(JSON, nullable=False, default=list)
    enrichment = Column(JSON, nullable=False, default=list)
    schedule = Column(JSON, nullable=False, default=dict)
    deliver = Column(JSON, nullable=False, default=dict)
    # Phase 2b: optional pre-foreclosure document-type selection (canonical
    # tokens, validated against the capability registry). NULL = legacy behavior
    # (today's full output); a non-empty list NARROWS to the chosen types.
    doc_types = Column(JSON, nullable=True)
    skip_trace_enabled = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # PERF (migration 033): the get_results sibling lookup filters on
    # user_id + lower(county) + upper(state) + record_type, so this is an
    # expression index (a plain btree on county/state would not be used).
    __table_args__ = (
        Index(
            "ix_scraper_configs_user_county_state_type",
            "user_id",
            func.lower(county),
            func.upper(state),
            "record_type",
        ),
    )

    user = relationship("User", back_populates="scraper_configs")
    jobs = relationship("Job", back_populates="scraper_config", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scraper_config_id = Column(UUID(as_uuid=False), ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    trigger = Column(String(32), nullable=False, default="manual")  # manual | scheduled | test
    page_current = Column(Integer, nullable=False, default=0)
    page_total = Column(Integer, nullable=False, default=0)
    record_count = Column(Integer, nullable=False, default=0)
    export_key = Column(String(512), nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    date_from = Column(String(16), nullable=True)   # resolved MM/DD/YYYY
    date_to = Column(String(16), nullable=True)     # resolved MM/DD/YYYY
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Phase 5 (migration 039): set once the dialer-push sweep has handled this
    # job (after skip-trace settles). NULL = not yet evaluated; the sweep claims
    # only done jobs with a dialer_webhook_url whose skip-trace is settled and
    # this is NULL, so each job is pushed at most once.
    dialer_pushed_at = Column(DateTime(timezone=True), nullable=True)

    # PERF (migration 033): list_jobs filters user_id and orders by created_at.
    __table_args__ = (
        Index("ix_jobs_user_created", "user_id", "created_at"),
    )

    user = relationship("User", back_populates="jobs")
    scraper_config = relationship("ScraperConfig", back_populates="jobs")
    results = relationship("Result", back_populates="job", cascade="all, delete-orphan")
    logs = relationship("JobLog", back_populates="job", cascade="all, delete-orphan")


class Result(Base):
    __tablename__ = "results"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date_recorded = Column(String(32), nullable=True)
    party_name = Column(String(512), nullable=True)
    heirs = Column(Text, nullable=True)
    legal_description = Column(Text, nullable=True)
    doc_type = Column(String(128), nullable=True)  # Phase 2a: which document produced this lead
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    mailing_address = Column(String(512), nullable=True)
    enrichment_data = Column(JSON, nullable=True, default=dict)
    # Skip trace (Sprint 4, Tracerfy): populated asynchronously by the
    # skip-trace dispatcher + webhook ingest. Status transitions:
    # not_attempted → queued → submitted → hit | miss | errored.
    phone = Column(String(32), nullable=True)
    phone_type = Column(String(16), nullable=True)  # Mobile | Landline | VoIP
    phone_dnc_flag = Column(Boolean, nullable=True)
    email = Column(String(255), nullable=True)
    # Multi-contact (up to 3). The scalar phone/email above stay the PRIMARY
    # (= phones[0]/emails[0]) for all existing consumers; these add the extras
    # for display. NULL = legacy/not-yet-traced; [] = traced, none found.
    phones = Column(JSON, nullable=True)  # [{"number": str, "type": str|None}]
    emails = Column(JSON, nullable=True)  # [str]
    skip_trace_status = Column(String(16), nullable=False, default="not_attempted")
    skip_trace_attempted_at = Column(DateTime(timezone=True), nullable=True)
    # Sprint 6.4: cross-job deduplication
    dedup_hash = Column(String(64), nullable=True, index=True)
    is_duplicate = Column(Boolean, nullable=False, default=False)
    raw_html_hash = Column(String(32), nullable=True, index=True)
    # Phase 3 (migration 037): post-enrichment sha256(parcel|address), the SAME
    # key property_list_membership stores (property_identity.compute_property_key).
    # NULL for weak-identity rows. Lets the combine/overlap export join overlap
    # property_keys back to full Result rows. dedup_hash is PRE-enrichment and
    # can differ, so it cannot serve this join.
    property_key = Column(String(64), nullable=True)
    # Phase 4 (migration 038): structured tax-delinquency fields for amount/age
    # filtering. Populated SOURCE-GATED (King Socrata tax_delinquent only);
    # other counties' tax rows stay NULL and never match a filter. bill_year is
    # stored (stable); "months delinquent" is derived at query time from it.
    delinquent_amount = Column(Numeric(12, 2), nullable=True)
    delinquent_bill_year = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # PERF (migration 033): get_results filters job_id + user_id, aggregates
    # by is_duplicate, and orders by (is_duplicate, created_at).
    # Phase 3 (migration 037): partial index for the intersection export join
    # (WHERE user_id = :uid AND property_key = ANY(:keys)); null keys never
    # queried, so the index excludes them.
    __table_args__ = (
        Index(
            "ix_results_job_user_dup_created",
            "job_id",
            "user_id",
            "is_duplicate",
            "created_at",
        ),
        Index(
            "ix_results_user_property_key",
            "user_id",
            "property_key",
            postgresql_where=text("property_key IS NOT NULL"),
        ),
        # Phase 4 (migration 038): per-job tax filters over non-null structured rows.
        Index(
            "ix_results_job_delinquent_amount",
            "job_id",
            "delinquent_amount",
            postgresql_where=text("delinquent_amount IS NOT NULL"),
        ),
        Index(
            "ix_results_job_delinquent_year",
            "job_id",
            "delinquent_bill_year",
            postgresql_where=text("delinquent_bill_year IS NOT NULL"),
        ),
    )

    job = relationship("Job", back_populates="results")


class DeliveredRecord(Base):
    """Append-only log of (user_id, dedup_hash) pairs (Sprint 6.4).

    Unique constraint on (user_id, dedup_hash) — the first successful
    INSERT for a given hash wins. Subsequent scrape runs that produce
    the same parcel_id + property_address hash get an ON CONFLICT and
    the worker flags the new Result as is_duplicate=true.

    Duplicates are still stored in `results` (so the UI can show
    "3 duplicate leads filtered this run" for transparency) but they
    do NOT count against the user's monthly records quota.
    """

    __tablename__ = "delivered_records"
    __table_args__ = (
        UniqueConstraint("user_id", "dedup_hash", name="uq_delivered_records_user_hash"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dedup_hash = Column(String(64), nullable=False)
    first_result_id = Column(
        UUID(as_uuid=False),
        ForeignKey("results.id", ondelete="SET NULL"),
        nullable=True,
    )
    first_job_id = Column(UUID(as_uuid=False), nullable=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    first_delivered_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class PropertyListMembership(Base):
    """Phase 1: per-(user, record_type, property) rollup that powers Phase 3's
    cross-list overlap ("on both lists") as an indexed lookup instead of a
    self-join over results history.

    One row per (user_id, record_type, property_key). STRONG-IDENTITY ONLY:
    property_key is the post-enrichment sha256(parcel|address) from
    property_identity.compute_property_key; weak name/date rows are excluded.

    sighting_count is ADVISORY (cumulative scrape observations, not idempotent
    across job re-runs, never used for billing or correctness). Maintained by
    _upsert_property_membership in workers/tasks.py; pruned by purge_old_records.
    """

    __tablename__ = "property_list_membership"
    __table_args__ = (
        Index(
            "ix_property_list_membership_user_key",
            "user_id",
            "property_key",
        ),
    )

    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    record_type = Column(String(64), primary_key=True)
    property_key = Column(String(64), primary_key=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    sighting_count = Column(Integer, nullable=False, default=1)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CountyConnector(Base):
    __tablename__ = "county_connectors"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    county = Column(String(128), nullable=False, index=True)
    state = Column(String(2), nullable=False, index=True)
    record_types = Column(JSON, nullable=False, default=list)
    scraper_class = Column(String(255), nullable=False)
    scraper_mode = Column(String(16), nullable=False, default="ai")  # ai | manual
    render_mode = Column(String(16), nullable=False, default="playwright")  # playwright | static
    base_url = Column(String(512), nullable=False)
    gis_endpoint = Column(Text, nullable=True)  # Free ArcGIS REST API URL
    assessor_url = Column(Text, nullable=True)  # County assessor website (AI fallback)
    health_status = Column(String(16), nullable=False, default="unknown")  # healthy | degraded | down | unknown
    max_date_range_days = Column(Integer, nullable=True)  # null = unlimited, 30 = single-date portals
    last_checked = Column(DateTime(timezone=True), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # PERF (migration 033): the public /scrapers/connectors picker filters
    # active + health_status and orders by state, county.
    __table_args__ = (
        Index(
            "ix_county_connectors_picker",
            "active",
            "health_status",
            "state",
            "county",
        ),
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level = Column(String(16), nullable=False, default="info")  # info | success | warning | error
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    job = relationship("Job", back_populates="logs")


class CountyRecord(Base):
    __tablename__ = "county_records"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    county = Column(String(64), nullable=False, index=True)
    state = Column(String(4), nullable=False, index=True)
    doc_type = Column(String(128), nullable=True)
    date_recorded = Column(String(32), nullable=True)
    party_name = Column(String(512), nullable=True)
    heirs = Column(Text, nullable=True)
    legal_description = Column(Text, nullable=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    mailing_address = Column(String(512), nullable=True)
    enrichment_data = Column(JSON, nullable=True, default=dict)
    record_hash = Column(String(32), nullable=False, unique=True, index=True)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    batch_date = Column(Date, server_default=func.current_date(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserRecordView(Base):
    __tablename__ = "user_record_views"
    __table_args__ = (
        UniqueConstraint("user_id", "scraper_config_id", name="uq_user_scraper_view"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scraper_config_id = Column(UUID(as_uuid=False), ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    last_viewed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ─── Sprint 4: Skip trace (Tracerfy) ─────────────────────────────────────────

class SkipTraceCache(Base):
    """90-day address-keyed cache.

    When a parcel appears in a new scrape, the dispatcher first checks this
    cache. If there's a hit less than 90 days old, the phone/email are copied
    directly to the Result row — no Tracerfy credit consumed. Keyed on a
    SHA-256 hash of the normalized property address + city + state so that
    minor formatting variations resolve to the same cache key.
    """

    __tablename__ = "skip_trace_cache"

    address_hash = Column(String(64), primary_key=True)
    phone = Column(String(32), nullable=True)
    phone_type = Column(String(16), nullable=True)
    phone_dnc_flag = Column(Boolean, nullable=True)
    email = Column(String(255), nullable=True)
    # Multi-contact cache (up to 3), mirrors Result.phones/emails so a cache-hit
    # also populates the extras. NULL for pre-existing cache rows.
    phones = Column(JSON, nullable=True)  # [{"number": str, "type": str|None}]
    emails = Column(JSON, nullable=True)  # [str]
    raw_response = Column(JSON, nullable=True)
    fetched_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )


class SkipTraceQueue(Base):
    """One row per Tracerfy batch submission.

    Correlates Tracerfy's `queue_id` (integer) back to the BridgeLeads
    job that enqueued the rows, so the webhook receiver can find the
    right records when Tracerfy POSTs the completion payload.
    """

    __tablename__ = "skip_trace_queues"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    tracerfy_queue_id = Column(Integer, nullable=False, unique=True)
    job_id = Column(
        UUID(as_uuid=False),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_type = Column(String(16), nullable=False, default="normal")  # normal | advanced
    status = Column(String(16), nullable=False, default="pending", index=True)
    # pending | completed | errored
    rows_uploaded = Column(Integer, nullable=False, default=0)
    credits_deducted = Column(Integer, nullable=False, default=0)
    download_url = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class PendingSkipTraceRow(Base):
    """Work queue for the skip-trace dispatcher.

    Scrape jobs insert rows here during enrichment; the dispatcher
    (Celery Beat, every 5 min) drains the queue, groups by trace_type,
    and submits up to 2 batch POSTs per tick to stay under Tracerfy's
    10-POSTs-per-5-min rate limit.
    """

    __tablename__ = "pending_skip_trace_rows"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(
        UUID(as_uuid=False),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    result_id = Column(
        UUID(as_uuid=False),
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_address = Column(String(512), nullable=False)
    city = Column(String(128), nullable=True)
    state = Column(String(2), nullable=True)
    zip = Column(String(16), nullable=True)
    first_name = Column(String(128), nullable=True)
    last_name = Column(String(128), nullable=True)
    mail_address = Column(String(512), nullable=True)
    mail_city = Column(String(128), nullable=True)
    mail_state = Column(String(2), nullable=True)
    mail_zip = Column(String(16), nullable=True)
    trace_type = Column(String(16), nullable=False, default="normal")
    status = Column(String(16), nullable=False, default="queued", index=True)
    # queued | submitted | completed | errored
    tracerfy_queue_id = Column(Integer, nullable=True, index=True)
    enqueued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)

    # PERF (migration 033): the skip-trace dispatcher drains the queue by
    # filtering status + trace_type and ordering by enqueued_at.
    __table_args__ = (
        Index(
            "ix_pending_skip_trace_dispatch",
            "status",
            "trace_type",
            "enqueued_at",
        ),
    )


class SkipTraceMeterEvent(Base):
    """Transactional outbox for Stripe skip-trace MeterEvents.

    REDTEAM (Codex convergence — meter outbox): the Tracerfy ingest worker
    advances each user's skip_trace_used_this_month counter AND flips the
    SkipTraceQueue to "completed" in ONE committed transaction — that
    completed-status flip is the ingest replay-idempotency anchor, so a
    replay no-ops and the counter can never re-advance. The Stripe MeterEvent,
    however, used to be fired AFTER that commit via a fire-and-forget Celery
    enqueue whose inner Stripe call swallowed every exception (so transient
    Stripe failures were never retried) and whose enqueue was best-effort (so
    a broker-down-at-enqueue lost the event with no record to recover from).
    Either path left usage advanced locally but never billed — permanent
    under-billing with no recovery anchor.

    The fix is a durable outbox: report_usage_from_webhook INSERTs one row
    here per billable user inside the SAME transaction that advances the
    counter and flips the queue status, so the intent-to-bill is committed
    atomically with the usage it represents. A row stays with
    reported_at = NULL until report_skip_trace_meter_event successfully fires
    the Stripe MeterEvent and stamps reported_at. The inline post-commit
    enqueue is the fast path; the flush_skip_trace_meter_outbox beat task
    sweeps any rows whose enqueue was lost (broker down / worker crash).

    Idempotency: UNIQUE(tracerfy_queue_id, user_id) + ON CONFLICT DO NOTHING
    means re-running report_usage_from_webhook can never duplicate outbox
    rows, and the Stripe identifier is a stable (queue_id, user_id) string so
    retries and beat-recovery never double-bill.

    System table: written only by workers via system_sync_session() — like
    skip_trace_queues / county_records it carries NO user RLS policy.
    """

    __tablename__ = "skip_trace_meter_events"
    __table_args__ = (
        UniqueConstraint(
            "tracerfy_queue_id",
            "user_id",
            name="uq_skip_trace_meter_events_queue_user",
        ),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    tracerfy_queue_id = Column(Integer, nullable=False, index=True)
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    billable_units = Column(Integer, nullable=False)
    stripe_customer_id = Column(String(255), nullable=True)
    plan = Column(String(32), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # NULL until the Stripe MeterEvent is durably reported; the partial index
    # in migration 026 makes the "unreported" sweep cheap.
    reported_at = Column(DateTime(timezone=True), nullable=True)


# ─── Sprint 7.3: Referral program ────────────────────────────────────────────


class ReferralEvent(Base):
    """Append-only audit log of referral credit grants.

    One row per successful referee → paid conversion. The unique
    constraint on referee_id is the idempotency guard — if the Stripe
    webhook replays a checkout.session.completed event, the second
    INSERT conflicts and the bonus is not granted twice.
    """

    __tablename__ = "referral_events"
    __table_args__ = (
        UniqueConstraint("referee_id", name="uq_referral_events_referee"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    referrer_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    referee_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount_cents = Column(Integer, nullable=False)
    reason = Column(String(64), nullable=False)
    # e.g. "referee_converted_to_paid"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DialerDelivery(Base):
    """Per-contact outbox for native dialer connectors (Thread 3).

    The generic webhook push is a single batch POST, but bulk-less native dialers
    (e.g. PhoneBurner) require one POST per contact, so partial success is the norm
    (some delivered, some rate-limited/401). The job-level Job.dialer_pushed_at
    claim only means "outbox materialized" — durable per-contact state lives here so
    a replay re-sends ONLY the rows that aren't 'delivered', never re-pushing a
    contact that already landed (Codex).

    Materialized by dialer_push_sweep (one row per dialer-ready lead) and drained in
    bounded chunks by process_dialer_outbox. Credentials are NEVER stored here — the
    processor re-reads them from the scraper_config at send time.
    """

    __tablename__ = "dialer_deliveries"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(
        UUID(as_uuid=False),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    result_id = Column(
        UUID(as_uuid=False),
        ForeignKey("results.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    scraper_config_id = Column(
        UUID(as_uuid=False),
        ForeignKey("scraper_configs.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor_id = Column(String(32), nullable=False)
    # pending | delivered | failed
    status = Column(String(16), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(256), nullable=True)
    vendor_response_code = Column(Integer, nullable=True)
    vendor_contact_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One outbox row per (job, result) — idempotent materialization; a re-run
        # sweep can ON CONFLICT DO NOTHING instead of duplicating contacts.
        UniqueConstraint("job_id", "result_id", name="uq_dialer_delivery_job_result"),
        # The chunk drain + replay both scan pending/failed rows for a job.
        Index("ix_dialer_deliveries_job_status", "job_id", "status"),
    )
