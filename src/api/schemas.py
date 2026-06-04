from datetime import UTC, datetime
from typing import Any, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator

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

    @field_validator("webhook_url")
    @classmethod
    def webhook_url_format(cls, v: str | None) -> str | None:
        # Structural validation only — HTTPS scheme + a real host + length.
        # The authoritative SSRF check (DNS resolution against blocked IP
        # ranges) runs in the worker via validate_outbound_webhook()
        # immediately before the POST, so a config save never depends on
        # live DNS and a host that rebinds after save is still caught.
        if v is None or v == "":
            return None
        if len(v) > 2000:
            raise ValueError("webhook_url too long (max 2000 chars)")
        parsed = urlparse(v)
        if parsed.scheme != "https":
            raise ValueError("webhook_url must use https://")
        if not parsed.hostname:
            raise ValueError("webhook_url must include a host")
        return v

    @field_validator("webhook_secret")
    @classmethod
    def webhook_secret_length(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) < 24:
            raise ValueError("webhook_secret must be at least 24 characters")
        if len(v) > 256:
            raise ValueError("webhook_secret too long (max 256 chars)")
        return v


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
        self.deliver.setdefault("formats", ["csv"])
        self.deliver.setdefault("emails", [])
        self.deliver.setdefault("webhook_url", None)
        self.deliver.setdefault("webhook_secret", None)
        # Also normalize fields / enrichment / schedule defensively
        if isinstance(self.fields, list):
            self.fields = dict.fromkeys(self.fields, True)
        elif not isinstance(self.fields, dict):
            self.fields = {}
        if not isinstance(self.enrichment, dict):
            self.enrichment = {}
        if not isinstance(self.schedule, dict):
            self.schedule = {}


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
    status: str
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
        if self.status in ("done", "failed", "cancelled"):
            self.progress_pct = 100 if self.status == "done" else None
            self.estimated_seconds_remaining = 0
            self.estimated_time_remaining = "Done" if self.status == "done" else None
            self.progress_label = f"Complete — {self.record_count} records" if self.status == "done" else self.status.title()
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
        elif self.status == "scraping":
            self.progress_label = "Starting scrape..."
        elif self.status == "enriching":
            self.progress_label = "Enriching addresses..."
        elif self.status in ("pending", "queued"):
            self.progress_label = "Waiting to start..."
        elif self.status == "probing":
            self.progress_label = "Connecting to county portal..."


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
    skip_trace_status: str = "not_attempted"  # not_attempted|queued|submitted|hit|miss|errored
    skip_trace_attempted_at: datetime | None = None
    is_duplicate: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}

    def model_post_init(self, __context: Any) -> None:
        # Sanitize HTML entities from all string fields before API response
        for field in ("property_address", "mailing_address", "party_name", "heirs", "legal_description"):
            val = getattr(self, field, None)
            if val and isinstance(val, str):
                cleaned = val.replace("&nbsp;", "").replace("&amp;", "&").strip()
                object.__setattr__(self, field, cleaned if cleaned else None)


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


# ─── Live run (SSE) ───────────────────────────────────────────────────────────

class LogLine(BaseModel):
    id: str
    level: str           # info | success | warning | error
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ProgressEvent(BaseModel):
    page_current: int
    page_total: int
    record_count: int


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
