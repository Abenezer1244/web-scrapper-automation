import re
from datetime import UTC, date, datetime
from typing import Any, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from src.config.constants import BatchRunStatus, JobStatus

# ─── Auth ─────────────────────────────────────────────────────────────────────

def _validate_password_rules(v: str) -> str:
    """Shared password policy: min 10, max 72 chars.

    Single source of truth so UserRegister, PasswordChange, and the
    A3 reset flow (ResetPasswordRequest) enforce IDENTICAL rules — a
    reset must never be a back door around the registration policy.
    """
    if len(v) < 10:
        raise ValueError("Password must be at least 10 characters")
    if len(v) > 72:
        raise ValueError("Password must not exceed 72 characters")
    return v


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    # Sprint 7.3: optional referral code passed from the ?ref= URL
    # parameter. When present and valid, the new user gets linked to
    # the referrer and the referrer earns $20 credit on paid
    # conversion. Unknown/invalid codes are silently dropped — no
    # error to avoid leaking which codes exist.
    ref: str | None = None

    @field_validator("password")
    @classmethod
    def password_validation(cls, v: str) -> str:
        return _validate_password_rules(v)

    @field_validator("ref")
    @classmethod
    def ref_format(cls, v: str | None) -> str | None:
        if v is None or v == "" or len(v) > 64:
            return None  # bound raw input before normalization
        v = v.strip().upper()
        if len(v) > 16 or not v.isalnum():
            return None  # Silently drop malformed codes
        return v


class UserLogin(BaseModel):
    email: EmailStr
    # Bounded so a multi-MB body can't drive bcrypt CPU on the login path
    # (bcrypt only uses the first 72 bytes anyway).
    password: str = Field(max_length=72)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=72)  # bcrypt only uses first 72 bytes
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        return _validate_password_rules(v)


class ForgotPasswordRequest(BaseModel):
    """A3: request a password-reset link. Body for POST /auth/forgot-password.

    Only the email is needed. The endpoint is enumeration-safe — it
    ALWAYS returns 200 regardless of whether this address has an
    account — so no other field is accepted or revealed here.
    """
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """A3: complete a password reset. Body for POST /auth/reset-password.

    `token` is the short-lived single-use reset JWT (aud=bridgeleads-reset)
    minted by /auth/forgot-password. `new_password` is validated against
    the SAME policy as registration/change-password via the shared
    validator so the reset path cannot weaken the password rules.
    """
    token: str = Field(max_length=4096)  # bound — a JWT is ~hundreds of bytes
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_policy(cls, v: str) -> str:
        return _validate_password_rules(v)


class MfaSetupResponse(BaseModel):
    """POST /auth/mfa/setup — returns the new TOTP secret (manual entry) and an
    otpauth:// provisioning URI for QR. Shown once; not yet enabled."""
    secret: str
    provisioning_uri: str


class MfaEnableRequest(BaseModel):
    """Confirm enrollment with a current TOTP code from the authenticator."""
    code: str = Field(min_length=6, max_length=10)


class MfaEnableResponse(BaseModel):
    """One-time backup codes, shown exactly once at enable time."""
    backup_codes: list[str]


class MfaDisableRequest(BaseModel):
    """Disabling MFA requires the password AND a second factor (TOTP or a
    backup code) — knowledge of the password alone must not remove MFA."""
    password: str = Field(max_length=72)
    # 6-digit TOTP or an 80-bit base32 backup code 'xxxx-xxxx-xxxx-xxxx' (19 chars).
    code: str = Field(min_length=6, max_length=32)


class MfaStatusResponse(BaseModel):
    enabled: bool


class MfaLoginRequest(BaseModel):
    """POST /auth/login/mfa — redeem the login MFA challenge. `mfa_token` is the
    short-lived challenge token returned by /auth/login when MFA is enabled;
    `code` is a 6-digit TOTP or an 80-bit base32 backup code."""
    mfa_token: str = Field(max_length=4096)  # bound — a JWT is ~hundreds of bytes
    code: str = Field(min_length=6, max_length=32)


class BreakGlassLoginRequest(BaseModel):
    """POST /auth/login/break-glass — redeem an operator-issued break-glass code.
    Reuses the /auth/login challenge token; `code` is a 128-bit 'bg-' code which
    renders as 35 chars ('bg-' + 26 base32 chars grouped by dashes), so it needs a
    larger cap than MfaLoginRequest's 32 (Codex)."""
    mfa_token: str = Field(max_length=4096)
    code: str = Field(min_length=6, max_length=64)


class UserResponse(BaseModel):
    id: str
    email: str
    plan: str
    records_used: int
    records_limit: int
    is_admin: bool = False
    trial_ends_at: datetime | None = None
    is_trial: bool = False
    trial_days_remaining: int | None = None
    notification_prefs: dict[str, bool] = {}
    created_at: datetime

    model_config = {"from_attributes": True}

    def model_post_init(self, __context: Any) -> None:
        if self.trial_ends_at:
            now = datetime.now(UTC)
            ends = self.trial_ends_at if self.trial_ends_at.tzinfo else self.trial_ends_at.replace(tzinfo=UTC)
            remaining = (ends - now).total_seconds()
            if remaining > 0:
                self.is_trial = True
                self.trial_days_remaining = max(0, int(remaining / 86400))


class NotificationPrefsUpdate(BaseModel):
    """Allowlisted notification toggles. Every field is optional so the client
    can send a partial update; unknown keys are rejected (extra='forbid') so a
    caller can't stuff arbitrary data into the JSON column."""

    job_completed: bool | None = None
    job_failed: bool | None = None
    new_records: bool | None = None
    usage_alert: bool | None = None
    payment_failed: bool | None = None

    model_config = {"extra": "forbid"}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 3600


class LoginResponse(BaseModel):
    """POST /auth/login (and /auth/login/mfa) response. Two mutually exclusive
    shapes, discriminated by `mfa_required`:
    - mfa_required=False → login complete: access_token + refresh_token are
      ALWAYS populated. This is the only shape a non-MFA account ever sees, so
      the prior no-MFA runtime contract is unchanged.
    - mfa_required=True → password OK but MFA enabled: access_token/refresh_token
      are null and a short-lived mfa_token is returned to redeem at
      POST /auth/login/mfa.
    The fields are Optional (vs TokenResponse's required access_token) only to
    model the challenge shape — clients must branch on mfa_required."""
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 3600
    mfa_required: bool = False
    mfa_token: str | None = None


class ApiKeyResponse(BaseModel):
    api_key: str  # Raw key — shown once, then lost


# ─── Scraper Configs ──────────────────────────────────────────────────────────

class ScheduleConfig(BaseModel):
    frequency: str = Field(default="manual", max_length=16)      # manual | daily | weekly | monthly
    run_at_hour: int = Field(default=6, ge=0, le=23)             # UTC
    run_at_minute: int = Field(default=0, ge=0, le=59)
    date_range_mode: str = Field(default="rolling_90", max_length=24)  # rolling_90 | custom | since_last_run
    date_from: str | None = Field(default=None, max_length=32)   # ISO date string
    date_to: str | None = Field(default=None, max_length=32)

    @field_validator("frequency")
    @classmethod
    def known_frequency(cls, v: str) -> str:
        # A typo'd frequency ("dailyx") would persist and silently never fire
        # (both dispatchers skip anything that isn't an exact known value) —
        # reject at the boundary instead (Codex P2). date_range_mode stays
        # advisory for back-compat (legacy stored values exist).
        v = v.strip().lower()
        if v not in {"manual", "daily", "weekly", "monthly"}:
            raise ValueError("frequency must be manual, daily, weekly, or monthly")
        return v


def _validate_https_webhook_url(v: str | None) -> str | None:
    """Structural validation only — HTTPS scheme + a real host + length.
    The authoritative SSRF check (DNS resolution against blocked IP ranges)
    runs in the worker via validate_outbound_webhook() immediately before the
    POST, so a config save never depends on live DNS and a host that rebinds
    after save is still caught."""
    if v is None or v == "":
        return None
    if len(v) > 2000:
        raise ValueError("webhook url too long (max 2000 chars)")
    parsed = urlparse(v)
    if parsed.scheme != "https":
        raise ValueError("webhook url must use https://")
    if not parsed.hostname:
        raise ValueError("webhook url must include a host")
    return v


def _validate_webhook_secret(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if len(v) < 24:
        raise ValueError("webhook secret must be at least 24 characters")
    if len(v) > 256:
        raise ValueError("webhook secret too long (max 256 chars)")
    return v


class DeliverConfig(BaseModel):
    emails: list[EmailStr] = Field(default_factory=list, max_length=10)
    formats: list[str] = Field(default=["csv"], max_length=5)  # csv | excel | json (one or more)
    webhook_url: str | None = None
    # Sprint 6.5: optional HMAC-SHA256 shared secret. When set, every
    # webhook POST carries an `X-BridgeLeads-Signature` header and
    # `signature` field that the consumer can verify against the secret
    # using sha256(secret, canonical_json_payload). Empty = unsigned
    # (relies on URL secrecy alone). Min 24 chars when set.
    webhook_secret: str | None = None
    # Phase 5: generic "push to any dialer". SEPARATE from webhook_url — this
    # pushes dialer-ready LEAD ROWS (valid phone + not-DNC) to the user's dialer
    # / Zapier catch-hook, not a job summary. Its own secret (no fallback to
    # webhook_secret). Same HTTPS + SSRF-at-send-time guarantees.
    dialer_webhook_url: str | None = None
    dialer_webhook_secret: str | None = None
    # Thread 3: which dialer connector handles the push. None = the shipped
    # generic webhook/Zapier connector. Validated against the server-side
    # allowlist so an arbitrary string can never reach connector dispatch.
    dialer_type: str | None = None
    # Thread 3: PhoneBurner native connector credentials (used when
    # dialer_type == "phoneburner"). The access token is a secret — write-only in
    # responses (redacted in ScraperConfigResponse, like the HMAC secrets).
    phoneburner_access_token: str | None = None
    phoneburner_owner_id: str | None = None

    @field_validator("dialer_type")
    @classmethod
    def dialer_type_allowlisted(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from src.config.constants import REGISTERED_DIALER_VENDOR_IDS
        if v not in REGISTERED_DIALER_VENDOR_IDS:
            raise ValueError(f"Unknown dialer_type: {v!r}")
        return v

    @field_validator("phoneburner_access_token")
    @classmethod
    def validate_pb_token(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.strip() or len(v) > 4096 or any(c in v for c in "\r\n\t"):
            raise ValueError("phoneburner_access_token is empty or malformed")
        return v

    @field_validator("phoneburner_owner_id")
    @classmethod
    def validate_pb_owner(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        if not s or len(s) > 64 or any(c in s for c in "\r\n\t"):
            raise ValueError("phoneburner_owner_id is empty or malformed")
        return s

    @field_validator("emails")
    @classmethod
    def limit_recipients(cls, v: list) -> list:
        if len(v) > 10:
            raise ValueError("Maximum 10 delivery email addresses")
        return v

    @field_validator("formats")
    @classmethod
    def bound_formats(cls, v: list[str]) -> list[str]:
        if any(len(f) > 16 for f in v):
            raise ValueError("invalid format value")
        return v

    @field_validator("webhook_url", "dialer_webhook_url")
    @classmethod
    def webhook_url_format(cls, v: str | None) -> str | None:
        return _validate_https_webhook_url(v)

    @field_validator("webhook_secret", "dialer_webhook_secret")
    @classmethod
    def webhook_secret_length(cls, v: str | None) -> str | None:
        return _validate_webhook_secret(v)

    @model_validator(mode="after")
    def require_connector_credentials(self) -> "DeliverConfig":
        # Reject an unusable native-dialer config up front (Codex) rather than
        # accepting it and failing every job's contacts later in the outbox.
        if self.dialer_type == "phoneburner":
            if not self.phoneburner_access_token or not self.phoneburner_owner_id:
                raise ValueError(
                    "dialer_type 'phoneburner' requires phoneburner_access_token "
                    "and phoneburner_owner_id"
                )
        return self


class FieldsConfig(BaseModel):
    party_name: bool = True
    parcel_id: bool = True
    property_address: bool = True
    mailing_address: bool = True
    heirs: bool = True
    legal_description: bool = False
    date_recorded: bool = True

    model_config = {"extra": "forbid"}


class EnrichmentConfig(BaseModel):
    property_lookup: bool = True
    skip_tracing: bool = False


# TypedDict shapes for the same configs as stored in JSON columns on the
# ScraperConfig ORM row. The Pydantic models above validate inputs at the
# API boundary; workers and the scheduler read the persisted dicts and
# need static type help to spot typos like config.schedule.get("date_rage_mode").
# total=False because each call site reads a different subset and missing
# keys are expected (the readers default via .get(key, fallback)).
class FieldsConfigDict(TypedDict, total=False):
    party_name: bool
    parcel_id: bool
    property_address: bool
    mailing_address: bool
    heirs: bool
    legal_description: bool
    date_recorded: bool


class EnrichmentConfigDict(TypedDict, total=False):
    property_lookup: bool
    skip_tracing: bool


class ScheduleConfigDict(TypedDict, total=False):
    frequency: str           # one of: manual, daily, weekly, monthly
    run_at_hour: int
    run_at_minute: int
    date_range_mode: str     # one of: rolling_90, custom, since_last_run
    date_from: str | None
    date_to: str | None


class DeliverConfigDict(TypedDict, total=False):
    emails: list[str]
    formats: list[str]       # subset of: csv, excel, json
    webhook_url: str | None
    webhook_secret: str | None
    dialer_webhook_url: str | None       # Phase 5: generic dialer push
    dialer_webhook_secret: str | None
    dialer_type: str | None              # Thread 3: dialer connector id (None = generic)
    phoneburner_access_token: str | None  # Thread 3: PhoneBurner OAuth token (write-only)
    phoneburner_owner_id: str | None


class ScraperConfigCreate(BaseModel):
    name: str = Field(max_length=120)
    county: str = Field(max_length=64)
    state: str = Field(max_length=16)
    record_type: str = Field(max_length=64)
    fields: FieldsConfig = FieldsConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    deliver: DeliverConfig = DeliverConfig()
    # Sprint 4: skip trace opt-in. Default False to avoid accidental
    # charges. Backend rejects with 402 if the user's plan is 'starter'.
    skip_trace_enabled: bool = False
    # Phase 2b: optional pre-foreclosure document-type selection. SHAPE ONLY
    # here — the canonical-value + per-county availability check lives in the
    # route (so we don't import scraper code into schemas). None = legacy/full
    # output; a non-empty list narrows. Bounded to prevent abuse.
    doc_types: list[str] | None = Field(default=None, max_length=10)

    @field_validator("state")
    @classmethod
    def state_uppercase(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2:
            raise ValueError("state must be a 2-letter code")
        return v

    @field_validator("county", "record_type")
    @classmethod
    def lowercase_slugs(cls, v: str) -> str:
        return v.lower().strip()


class ScraperConfigResponse(BaseModel):
    id: str
    user_id: str
    name: str
    county: str
    state: str
    record_type: str
    fields: dict[str, Any] | list[str] | Any
    enrichment: dict[str, Any] | Any
    schedule: dict[str, Any] | Any
    deliver: dict[str, Any] | Any
    skip_trace_enabled: bool = False
    doc_types: list[str] | None = None  # Phase 2b: pre-foreclosure doc-type selection (None = legacy)
    active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    def model_post_init(self, __context: Any) -> None:
        # Backfill missing deliver keys so the frontend never has to
        # defensively access `deliver.formats?.map(...)`. Three Sprint
        # 4 verification configs were created with `deliver = {}` and
        # crashed the /scrapers and /deliver pages. This normalizer
        # runs on every response so any legacy config gets reasonable
        # defaults on read without a migration.
        if not isinstance(self.deliver, dict):
            self.deliver = {}
        else:
            # Shallow-copy so the redaction below never mutates the ORM-attached
            # JSON dict (defensive — GETs don't commit, but don't risk it).
            self.deliver = dict(self.deliver)
        self.deliver.setdefault("formats", ["csv"])
        self.deliver.setdefault("emails", [])
        self.deliver.setdefault("webhook_url", None)
        # Security (Codex): HMAC secrets are WRITE-ONLY — never return them. A
        # stolen frontend token / XSS could otherwise read them and forge signed
        # webhook/dialer payloads. Expose only presence flags so the UI can show
        # "secret set" without leaking the value.
        self.deliver["webhook_secret_set"] = bool(self.deliver.get("webhook_secret"))
        self.deliver["dialer_webhook_secret_set"] = bool(self.deliver.get("dialer_webhook_secret"))
        # Thread 3: the PhoneBurner OAuth token is a credential — write-only too.
        self.deliver["phoneburner_access_token_set"] = bool(self.deliver.get("phoneburner_access_token"))
        self.deliver.pop("webhook_secret", None)
        self.deliver.pop("dialer_webhook_secret", None)
        self.deliver.pop("phoneburner_access_token", None)
        # Also normalize fields / enrichment / schedule defensively
        if isinstance(self.fields, list):
            self.fields = dict.fromkeys(self.fields, True)
        elif not isinstance(self.fields, dict):
            self.fields = {}
        if not isinstance(self.enrichment, dict):
            self.enrichment = {}
        if not isinstance(self.schedule, dict):
            self.schedule = {}


# ─── Batch scrape (Piece 2) ─────────────────────────────────────────────────

class BatchCreateRequest(BaseModel):
    """Create a batch scrape: multiple counties x record types under one parent,
    sharing fields/enrichment/deliver. Fans out into N child scrapes. The batch
    owns delivery (one combined CSV); per-child delivery is suppressed.

    2B: `schedule` (same validated shape as single scrapers) makes the batch
    recur — the scheduler beat creates a new run when the schedule fires.
    Default (frequency='manual') = 2A behavior: one immediate run, no recurrence.
    Children always store schedule={} — the PARENT owns scheduling, and
    dispatch_scheduled_jobs skips frequency='manual' children."""

    name: str | None = Field(default=None, max_length=120)
    state: str = Field(max_length=16)
    counties: list[str] = Field(min_length=1, max_length=250)
    record_types: list[str] = Field(min_length=1, max_length=10)
    fields: FieldsConfig = FieldsConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    deliver: DeliverConfig = DeliverConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    skip_trace_enabled: bool = False

    @field_validator("state")
    @classmethod
    def state_uppercase(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2:
            raise ValueError("state must be a 2-letter code")
        return v

    @field_validator("counties", "record_types")
    @classmethod
    def dedupe_slugs(cls, v: list[str]) -> list[str]:
        cleaned = sorted({s.strip().lower() for s in v if s and s.strip()})
        if not cleaned:
            raise ValueError("at least one value required")
        return cleaned


class BatchCreateResponse(BaseModel):
    batch_id: str
    child_count: int  # number of (county x record_type) scrapes launched
    status: BatchRunStatus  # "pending" — the run + child jobs are created async by the worker


class BatchRunResponse(BaseModel):
    id: str
    batch_id: str
    status: BatchRunStatus  # pending | running | done | partial | failed | cancelled
    child_job_ids: list[str] = []
    excluded_no_date_count: int = 0
    failed_children: list[dict[str, Any]] | None = None
    combined_export_ready: bool = False  # presence flag — never expose the R2 key
    created_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class BatchChildSummary(BaseModel):
    """One child scrape within a batch (county x record_type) and its job status."""

    config_id: str
    county: str
    record_type: str
    job_id: str | None = None  # None until the dispatch worker creates the job
    status: JobStatus = JobStatus.PENDING  # a child IS a job, so it uses the job state machine
    record_count: int = 0


class BatchSummaryResponse(BaseModel):
    """A batch + its (single, on-demand 2A) run status — for the list view."""

    id: str
    name: str | None = None
    state: str
    run_status: BatchRunStatus = BatchRunStatus.PENDING
    child_count: int = 0
    combined_export_ready: bool = False  # presence flag — never expose the R2 key
    created_at: datetime
    completed_at: datetime | None = None


class BatchDetailResponse(BatchSummaryResponse):
    """A batch with its per-child summary + failed-child detail — for the run view."""

    failed_children: list[dict[str, Any]] | None = None
    children: list[BatchChildSummary] = Field(default_factory=list)


# ─── Jobs ─────────────────────────────────────────────────────────────────────

class JobCreate(BaseModel):
    scraper_config_id: str = Field(max_length=64)  # UUID string
    trigger: str = Field(default="manual", max_length=16)  # manual | test

    @field_validator("trigger")
    @classmethod
    def valid_trigger(cls, v: str) -> str:
        if v not in {"manual", "test"}:
            raise ValueError("trigger must be manual or test")
        return v


class JobResponse(BaseModel):
    id: str
    user_id: str
    scraper_config_id: str
    status: JobStatus
    trigger: str
    page_current: int
    page_total: int
    record_count: int
    export_key: str | None
    error_message: str | None
    retry_count: int
    date_from: str | None = None       # resolved scrape date range
    date_to: str | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    # Scraper config context — avoids separate lookup on frontend
    scraper_name: str | None = None
    county: str | None = None
    state: str | None = None
    record_type: str | None = None
    # Computed progress fields (not stored in DB)
    progress_pct: int | None = None
    estimated_total_records: int | None = None
    estimated_seconds_remaining: int | None = None
    estimated_time_remaining: str | None = None  # "2m 30s", "45s", "Done"
    elapsed_seconds: int | None = None
    elapsed_time: str | None = None  # "1m 15s"
    progress_label: str | None = None  # "Scraping page 3 of 5", "Looking up parcels 42/100"

    model_config = {"from_attributes": True}

    @staticmethod
    def _fmt_time(secs: int | None) -> str | None:
        if secs is None:
            return None
        if secs <= 0:
            return "0s"
        mins, s = divmod(secs, 60)
        if mins > 0:
            return f"{mins}m {s}s"
        return f"{s}s"

    def model_post_init(self, __context: Any) -> None:
        now = datetime.now(UTC)

        # Elapsed time
        if self.started_at:
            started = self.started_at if self.started_at.tzinfo else self.started_at.replace(tzinfo=UTC)
            self.elapsed_seconds = max(0, int((now - started).total_seconds()))
            self.elapsed_time = self._fmt_time(self.elapsed_seconds)

        # Terminal states: 100% done, no estimate needed
        if self.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            self.progress_pct = 100 if self.status == JobStatus.DONE else None
            self.estimated_seconds_remaining = 0
            self.estimated_time_remaining = "Done" if self.status == JobStatus.DONE else None
            self.progress_label = (
                f"Complete — {self.record_count} records"
                if self.status == JobStatus.DONE
                else self.status.value.title()
            )
            return

        # Progress based on page_current / page_total
        if self.page_total > 0 and self.page_current > 0:
            self.progress_pct = min(99, int(self.page_current / self.page_total * 100))
            self.progress_label = f"Page {self.page_current} of {self.page_total}"

            # Estimate total records: (records so far / pages done) * total pages
            if self.record_count > 0:
                self.estimated_total_records = int(
                    self.record_count / self.page_current * self.page_total
                )

            # Estimate time remaining: (elapsed / pages done) * pages left
            if self.elapsed_seconds and self.elapsed_seconds > 0:
                secs_per_page = self.elapsed_seconds / self.page_current
                pages_left = self.page_total - self.page_current
                self.estimated_seconds_remaining = max(0, int(secs_per_page * pages_left))
                self.estimated_time_remaining = self._fmt_time(self.estimated_seconds_remaining)
        elif self.status == JobStatus.SCRAPING:
            self.progress_label = "Starting scrape..."
        elif self.status == JobStatus.ENRICHING:
            self.progress_label = "Enriching addresses..."
        elif self.status in (JobStatus.PENDING, JobStatus.QUEUED):
            self.progress_label = "Waiting to start..."
        elif self.status == JobStatus.PROBING:
            self.progress_label = "Connecting to county portal..."


class PhoneContact(BaseModel):
    """One skip-traced phone. ``type`` is Mobile|Landline|VoIP|null."""
    number: str
    type: str | None = None


class ResultRow(BaseModel):
    id: str
    date_recorded: str | None
    party_name: str | None
    heirs: str | None
    legal_description: str | None
    doc_type: str | None = None
    parcel_id: str | None
    property_address: str | None
    mailing_address: str | None
    enrichment_data: dict[str, Any] | None
    # Sprint 4: skip trace fields (populated asynchronously via the
    # dispatcher + Tracerfy webhook ingest path)
    phone: str | None = None
    phone_type: str | None = None
    phone_dnc_flag: bool | None = None
    email: str | None = None
    # Multi-contact: up to 3 each. phone/email above remain the primary
    # (= phones[0]/emails[0]); these surface the extras for display.
    phones: list[PhoneContact] | None = None
    emails: list[str] | None = None
    skip_trace_status: str = "not_attempted"  # not_attempted|queued|submitted|hit|miss|errored
    skip_trace_attempted_at: datetime | None = None
    is_duplicate: bool = False
    # Phase 4: structured tax-delinquency fields (King tax_delinquent only; NULL
    # elsewhere). Surfaced so the results view can show + filter by them.
    delinquent_amount: float | None = None
    delinquent_bill_year: int | None = None
    # Tier 0 (migration 057): owner-location flags, straight passthrough from the
    # stored columns (tri-state True/False/None=unknown).
    property_state: str | None = None
    owner_state: str | None = None
    absentee_owner: bool | None = None
    out_of_state_owner: bool | None = None
    # NTS Tier 1 (migration 059): matched trustee-sale auction data (pre_foreclosure).
    auction_date: date | None = None
    default_amount: float | None = None
    nts_match_confidence: float | None = None
    created_at: datetime
    # Derived signals (Tier 0, src/utils/lead_signals.py): computed at serialize
    # time, never stored. Populated in model_post_init from the fields above.
    # tax-only: months_delinquent / wa_foreclosure_eligible (None/False off-tax).
    months_delinquent: int | None = None
    wa_foreclosure_eligible: bool = False
    freshness_days: int | None = None
    contactability_score: int = 0
    days_to_auction: int | None = None

    model_config = {"from_attributes": True}

    @field_validator("phones", mode="before")
    @classmethod
    def _clean_phones(cls, v: Any) -> Any:
        # Tolerate legacy/malformed JSON so one bad contact can't 500 the whole
        # authorized results page. Keep only {number:str} entries, cap at 3.
        if not isinstance(v, list):
            return None
        out: list[dict] = []
        for item in v:
            if isinstance(item, dict):
                num = item.get("number")
                if isinstance(num, str) and num.strip():
                    typ = item.get("type")
                    out.append({"number": num.strip(), "type": typ if isinstance(typ, str) else None})
            if len(out) >= 3:
                break
        return out

    @field_validator("emails", mode="before")
    @classmethod
    def _clean_emails(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return None
        return [e.strip() for e in v if isinstance(e, str) and e.strip()][:3]

    def model_post_init(self, __context: Any) -> None:
        # Sanitize HTML entities from all string fields before API response
        for field in ("property_address", "mailing_address", "party_name", "heirs", "legal_description"):
            val = getattr(self, field, None)
            if val and isinstance(val, str):
                cleaned = val.replace("&nbsp;", "").replace("&amp;", "&").strip()
                object.__setattr__(self, field, cleaned if cleaned else None)
        # Defensive cap: never expose more than 3 contacts even if a row somehow
        # stored more (the workers already cap at 3).
        if self.phones and len(self.phones) > 3:
            object.__setattr__(self, "phones", self.phones[:3])
        if self.emails and len(self.emails) > 3:
            object.__setattr__(self, "emails", self.emails[:3])
        # Derived signals: computed point-in-time from this row's own fields.
        # date_recorded_parsed isn't a ResultRow field, so freshness falls back to
        # parsing the date_recorded string (handled inside derive_signals).
        from datetime import UTC, datetime

        from src.utils.lead_signals import derive_signals
        sig = derive_signals(self, datetime.now(UTC).date())
        object.__setattr__(self, "months_delinquent", sig["months_delinquent"])
        object.__setattr__(self, "wa_foreclosure_eligible", sig["wa_foreclosure_eligible"])
        object.__setattr__(self, "freshness_days", sig["freshness_days"])
        object.__setattr__(self, "contactability_score", sig["contactability_score"])
        object.__setattr__(self, "days_to_auction", sig["days_to_auction"])


class ResultsPage(BaseModel):
    job_id: str
    total: int
    page: int
    page_size: int
    items: list[ResultRow]
    enriched_count: int = 0      # records with property_address filled
    enriching: bool = False       # True while background enrichment is running
    total_scraped: int = 0       # all records before dedup
    duplicate_count: int = 0     # records flagged as duplicate
    date_range_mode: str = ""    # rolling_90 | since_last_run | custom etc.
    previous_job_id: str | None = None  # most recent job with results (for "view previous" link)
    # NTS Tier 1: True if the JOB has ANY auction-matched lead (independent of the
    # current page/filter). The frontend gates the Auction Date / Default Owed
    # columns on this so they don't flicker by page when auction matches are sparse.
    has_auction_data: bool = False


# ─── Live run (SSE) ───────────────────────────────────────────────────────────

class LogLine(BaseModel):
    id: str
    level: str           # info | success | warning | error
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── County connectors ────────────────────────────────────────────────────────

class ConnectorCreate(BaseModel):
    county: str = Field(max_length=64)
    state: str = Field(max_length=16)
    record_types: list[str] = Field(max_length=20)
    base_url: str = Field(max_length=2000)
    scraper_mode: str = Field(default="ai", max_length=16)  # ai | manual
    gis_endpoint: str | None = Field(default=None, max_length=2000)  # Free ArcGIS REST API URL
    assessor_url: str | None = Field(default=None, max_length=2000)  # County assessor website (AI fallback)

    @field_validator("state")
    @classmethod
    def state_uppercase(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2:
            raise ValueError("state must be a 2-letter code")
        return v

    @field_validator("county")
    @classmethod
    def county_lowercase(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("record_types")
    @classmethod
    def bound_record_types(cls, v: list[str]) -> list[str]:
        if any(len(rt) > 64 for rt in v):
            raise ValueError("invalid record_type value")
        return v


class ConnectorResponse(BaseModel):
    id: str
    county: str
    state: str
    record_types: list[str]
    scraper_mode: str  # ai | manual
    render_mode: str
    base_url: str
    gis_endpoint: str | None = None
    assessor_url: str | None = None
    health_status: str
    max_date_range_days: int | None = None  # null = unlimited
    last_checked: datetime | None
    active: bool
    # Phase 2b: pre-foreclosure doc-type selector metadata (from the capability
    # registry), present only for counties that support selection. Populated by
    # the /connectors handler, not the ORM. None = no selector (legacy/hidden).
    pre_foreclosure_doc_types: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


# ─── Cached Records ──────────────────────────────────────────────────────────

class CachedRecordRow(BaseModel):
    id: str
    date_recorded: str | None = None
    party_name: str | None = None
    heirs: str | None = None
    doc_type: str | None = None
    legal_description: str | None = None
    parcel_id: str | None = None
    property_address: str | None = None
    mailing_address: str | None = None
    is_new: bool = False
    scraped_at: datetime | None = None

    model_config = {"from_attributes": True}


class CachedResultsPage(BaseModel):
    config_id: str
    county: str
    state: str
    total: int
    new_count: int
    cache_age: str | None = None
    cache_stale: bool = False
    page: int
    page_size: int
    items: list[CachedRecordRow]


# ─── Segments: combine / overlap (Phase 3) ────────────────────────────────────

# Record types are DB-driven (county_connectors.record_types) and open-ended —
# new connectors add new slugs (e.g. death_certificate). So segment requests are
# validated by SHAPE, not against a closed enum, matching ConnectorCreate's
# bound_record_types convention; an unknown slug simply yields no overlap rather
# than a 422 for a type the system actually supports (Codex review).
_RECORD_TYPE_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SegmentIntersectionRequest(BaseModel):
    """Intersection ('on both lists'): properties a user has on ALL of the
    selected record-type lists. STRONG-IDENTITY ONLY (parcel/address match);
    weak name/date rows are excluded by design (see property_identity).

    record_types must be 2+ distinct slugs — a single list cannot 'overlap'
    itself. counties is an optional lowercase filter.
    """
    record_types: list[str] = Field(min_length=2, max_length=10)
    counties: list[str] | None = Field(default=None, max_length=100)
    # Optional filing-date window (migration 049 date_recorded_parsed). lookback_days
    # is the preset path (server derives filing_from = today - days); filing_from/to
    # is the custom path (explicit wins if both supplied). When ANY is set, rows with
    # an unparseable/NULL filing date are excluded and reported via
    # excluded_no_date_count. None everywhere = today's all-time behavior (unchanged).
    lookback_days: int | None = Field(default=None, ge=1, le=3660)
    filing_from: date | None = None
    filing_to: date | None = None

    @field_validator("record_types")
    @classmethod
    def validate_record_types(cls, v: list[str]) -> list[str]:
        cleaned = sorted({t.strip().lower() for t in v if t and t.strip()})
        if len(cleaned) < 2:
            raise ValueError("intersection requires at least 2 distinct record types")
        bad = [t for t in cleaned if not _RECORD_TYPE_SLUG.match(t)]
        if bad:
            raise ValueError(f"invalid record_type value(s): {', '.join(bad)}")
        return cleaned

    @field_validator("counties")
    @classmethod
    def clean_counties(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        cleaned = sorted({c.strip().lower() for c in v if c and c.strip()})
        if any(len(c) > 64 for c in cleaned):
            raise ValueError("invalid county value")
        return cleaned or None

    @model_validator(mode="after")
    def _check_filing_window(self) -> "SegmentIntersectionRequest":
        # Reject an inverted explicit window (Codex P2). lookback_days vs explicit
        # filing_from/to precedence is resolved in the route (explicit wins) — see
        # _resolve_filing_window in routes/segments.py.
        if self.filing_from and self.filing_to and self.filing_from > self.filing_to:
            raise ValueError("filing_from must be on or before filing_to")
        return self


class SegmentLeadRow(BaseModel):
    """One representative lead per dedup bucket (best row chosen by the window
    function). `identity_strength` is "strong" for intersection (always) and
    per-row for union (strong = parcel/address match, weak = name/date)."""
    id: str
    date_recorded: str | None = None
    party_name: str | None = None
    parcel_id: str | None = None
    property_address: str | None = None
    mailing_address: str | None = None
    county: str | None = None
    state: str | None = None
    phone: str | None = None
    phone_type: str | None = None
    email: str | None = None
    matched_record_types: list[str]
    overlap_count: int
    identity_strength: str = "strong"


class SegmentIntersectionResponse(BaseModel):
    mode: str = "intersection"
    # SAY SO (design): intersection is strong-identity only.
    identity_strength: str = "strong"
    record_types: list[str]
    counties: list[str] | None = None
    property_count: int
    truncated: bool = False  # preview cap reached — export for the full set
    # Rows skipped because their filing date was unparseable/NULL. Only nonzero
    # when a filing-date window is active (windowed queries require a real date).
    excluded_no_date_count: int = 0
    rows: list[SegmentLeadRow]


class SegmentUnionRequest(BaseModel):
    """Union ('combine'): every lead a user has on ANY of the selected
    record-type lists, merged into one INCLUSIVE deduped set — strong rows
    deduped by property_key, weak rows by dedup_hash, rows with neither kept as
    singletons. NO lead is ever silently dropped (contrast intersection, which
    is strong-only). 1+ distinct slug (combining one list just dedupes it across
    counties/jobs). counties is an optional lowercase filter.
    """
    record_types: list[str] = Field(min_length=1, max_length=10)
    counties: list[str] | None = Field(default=None, max_length=100)
    # Optional filing-date window — see SegmentIntersectionRequest (same semantics).
    lookback_days: int | None = Field(default=None, ge=1, le=3660)
    filing_from: date | None = None
    filing_to: date | None = None

    @field_validator("record_types")
    @classmethod
    def validate_record_types(cls, v: list[str]) -> list[str]:
        cleaned = sorted({t.strip().lower() for t in v if t and t.strip()})
        if len(cleaned) < 1:
            raise ValueError("union requires at least 1 record type")
        bad = [t for t in cleaned if not _RECORD_TYPE_SLUG.match(t)]
        if bad:
            raise ValueError(f"invalid record_type value(s): {', '.join(bad)}")
        return cleaned

    @field_validator("counties")
    @classmethod
    def clean_counties(cls, v: list[str] | None) -> list[str] | None:
        if not v:
            return None
        cleaned = sorted({c.strip().lower() for c in v if c and c.strip()})
        if any(len(c) > 64 for c in cleaned):
            raise ValueError("invalid county value")
        return cleaned or None

    @model_validator(mode="after")
    def _check_filing_window(self) -> "SegmentUnionRequest":
        # Reject an inverted explicit window (Codex P2). Precedence (explicit wins
        # over lookback_days) resolved in routes/segments.py._resolve_filing_window.
        if self.filing_from and self.filing_to and self.filing_from > self.filing_to:
            raise ValueError("filing_from must be on or before filing_to")
        return self


class SegmentUnionResponse(BaseModel):
    mode: str = "union"
    # Mixed identity — per-row identity_strength on each lead (not segment-level).
    record_types: list[str]
    counties: list[str] | None = None
    lead_count: int
    truncated: bool = False  # preview cap reached — export for the full set
    # Rows skipped because their filing date was unparseable/NULL. Only nonzero
    # when a filing-date window is active (windowed queries require a real date).
    excluded_no_date_count: int = 0
    rows: list[SegmentLeadRow]
