from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, EmailStr, field_validator

# ─── Auth ─────────────────────────────────────────────────────────────────────

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
        if len(v) < 10:
            raise ValueError("Password must be at least 10 characters")
        if len(v) > 72:
            raise ValueError("Password must not exceed 72 characters")
        return v

    @field_validator("ref")
    @classmethod
    def ref_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        v = v.strip().upper()
        if len(v) > 16 or not v.isalnum():
            return None  # Silently drop malformed codes
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("Password must be at least 10 characters")
        if len(v) > 72:
            raise ValueError("Password must not exceed 72 characters")
        return v


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


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 3600


class ApiKeyResponse(BaseModel):
    api_key: str  # Raw key — shown once, then lost


# ─── Scraper Configs ──────────────────────────────────────────────────────────

class ScheduleConfig(BaseModel):
    frequency: str = "manual"           # manual | daily | weekly | monthly
    run_at_hour: int = 6                # 0–23 UTC
    run_at_minute: int = 0              # 0–59
    date_range_mode: str = "rolling_90" # rolling_90 | custom | since_last_run
    date_from: str | None = None
    date_to: str | None = None


class DeliverConfig(BaseModel):
    emails: list[EmailStr] = []
    formats: list[str] = ["csv"]    # csv | excel | json (one or more)
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

    @field_validator("webhook_url")
    @classmethod
    def webhook_url_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("webhook_url must start with http:// or https://")
        if len(v) > 2000:
            raise ValueError("webhook_url too long (max 2000 chars)")
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


class ScraperConfigCreate(BaseModel):
    name: str
    county: str
    state: str
    record_type: str
    fields: FieldsConfig = FieldsConfig()
    enrichment: EnrichmentConfig = EnrichmentConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    deliver: DeliverConfig = DeliverConfig()
    # Sprint 4: skip trace opt-in. Default False to avoid accidental
    # charges. Backend rejects with 402 if the user's plan is 'starter'.
    skip_trace_enabled: bool = False

    @field_validator("state")
    @classmethod
    def state_uppercase(cls, v: str) -> str:
        return v.upper()

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
    fields: dict[str, Any]
    enrichment: dict[str, Any]
    schedule: dict[str, Any]
    deliver: dict[str, Any]
    skip_trace_enabled: bool = False
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
        if not isinstance(self.fields, dict):
            self.fields = {}
        if not isinstance(self.enrichment, dict):
            self.enrichment = {}
        if not isinstance(self.schedule, dict):
            self.schedule = {}


# ─── Jobs ─────────────────────────────────────────────────────────────────────

class JobCreate(BaseModel):
    scraper_config_id: str
    trigger: str = "manual"  # manual | test

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
    county: str
    state: str
    record_types: list[str]
    base_url: str
    scraper_mode: str = "ai"  # ai | manual
    gis_endpoint: str | None = None  # Free ArcGIS REST API URL
    assessor_url: str | None = None  # County assessor website (AI fallback)

    @field_validator("state")
    @classmethod
    def state_uppercase(cls, v: str) -> str:
        return v.upper()

    @field_validator("county")
    @classmethod
    def county_lowercase(cls, v: str) -> str:
        return v.lower().strip()


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
    last_checked: datetime | None
    active: bool

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
