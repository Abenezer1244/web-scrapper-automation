import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any, TypedDict
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from src.config.constants import BatchRunStatus, JobStatus, NotificationType

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


# Display-name bounds. 120 code points is generous for any real name; the
# 255-byte cap guards the encrypted-column path against multi-byte abuse.
_NAME_MAX_CHARS = 120
_NAME_MAX_BYTES = 255
# Coarse pre-normalization bound so a pathological multi-MB string can't drive
# normalization/regex CPU before the precise checks run.
_NAME_RAW_MAX = 1000


def _validate_display_name(v: str | None) -> str | None:
    """Sanitize a self-entered display name. Single source of truth so
    registration (UserRegister) and edit (ProfileUpdate) enforce IDENTICAL
    rules. Returns None when empty after stripping — the column is never stored
    as "" and the greeting falls back cleanly to no personal identifier.

    Per the security review: NFC-normalize, collapse any run of Unicode
    whitespace (tabs/newlines/nbsp included) to a single ASCII space, then reject
    any remaining control/format char. After whitespace is collapsed, a leftover
    category-C char is a non-whitespace control (NUL/escape), a Unicode bidi
    override (U+202A-202E / U+2066-2069) or a zero-width/invisible formatter
    (U+200B/C/D, U+FEFF) — all UI-spoofing footguns with no display-name value.
    Length is bounded in both code points and UTF-8 bytes.
    """
    if v is None:
        return None
    if len(v) > _NAME_RAW_MAX:
        raise ValueError("Name is too long")
    v = unicodedata.normalize("NFC", v)
    v = re.sub(r"\s+", " ", v).strip()
    if not v:
        return None  # empty after strip => NULL, never stored as ""
    if any(unicodedata.category(ch).startswith("C") for ch in v):
        raise ValueError("Name contains disallowed characters")
    if len(v) > _NAME_MAX_CHARS:
        raise ValueError(f"Name must not exceed {_NAME_MAX_CHARS} characters")
    if len(v.encode("utf-8")) > _NAME_MAX_BYTES:
        raise ValueError("Name is too long")
    return v


def _validate_required_name(v: str | None, label: str) -> str:
    """Required variant of _validate_display_name for first/last name. Same
    sanitization + bounds, but blank/empty after normalization is a 422 (the
    field is mandatory) rather than None. Single source of truth so registration
    and the profile/gate edit enforce identical rules per field."""
    cleaned = _validate_display_name(v)
    if cleaned is None:
        raise ValueError(f"{label} is required")
    return cleaned


class UserRegister(BaseModel):
    email: EmailStr
    password: str
    # Required first + last name (the dashboard greeting uses the first name).
    # Sanitized + bounded per-field through the SAME validator as the profile
    # edit so signup can't bypass the edit-time rules.
    first_name: str
    last_name: str
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

    @field_validator("first_name")
    @classmethod
    def first_name_validation(cls, v: str) -> str:
        return _validate_required_name(v, "First name")

    @field_validator("last_name")
    @classmethod
    def last_name_validation(cls, v: str) -> str:
        return _validate_required_name(v, "Last name")

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
    # First + last name. None for legacy users created before the required-name
    # gate; the greeting uses first_name and NEVER derives a name from the email.
    # profile_complete is the SERVER-OWNED truth the frontend gate keys on (so the
    # "both names present" rule lives in one place and can't drift).
    first_name: str | None = None
    last_name: str | None = None
    profile_complete: bool = False
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
        # Server-owned profile-complete rule: both names present and non-blank.
        # Strip so a whitespace-only value that slipped in via any non-validated
        # path (manual/import) does NOT read as complete (Codex hardening).
        self.profile_complete = bool(
            (self.first_name or "").strip() and (self.last_name or "").strip()
        )
        if self.trial_ends_at:
            now = datetime.now(UTC)
            ends = self.trial_ends_at if self.trial_ends_at.tzinfo else self.trial_ends_at.replace(tzinfo=UTC)
            remaining = (ends - now).total_seconds()
            if remaining > 0:
                self.is_trial = True
                self.trial_days_remaining = max(0, int(remaining / 86400))


class ProfileUpdate(BaseModel):
    """Editable profile fields — used by BOTH Settings>Account and the
    required-name gate. first_name + last_name are REQUIRED (non-empty after
    sanitization) so this endpoint is how an incomplete-profile user satisfies
    the gate. Sanitized through the SAME validator as registration. extra='forbid'
    so a caller cannot smuggle other user columns (plan, records_limit,
    is_admin, ...) through the profile endpoint."""

    first_name: str
    last_name: str

    model_config = {"extra": "forbid"}

    @field_validator("first_name")
    @classmethod
    def first_name_validation(cls, v: str) -> str:
        return _validate_required_name(v, "First name")

    @field_validator("last_name")
    @classmethod
    def last_name_validation(cls, v: str) -> str:
        return _validate_required_name(v, "Last name")


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


class RegisterResponse(BaseModel):
    """POST /auth/register response when EMAIL_VERIFICATION_ENABLED is on.

    Enumeration-safe: the SAME neutral body is returned whether the email is new
    (a verification link was sent) or already registered (a 'you already have an
    account' note was sent). No tokens — the account is created only after the
    verification link is redeemed at /auth/verify-email. `verification_required`
    lets the frontend show a 'check your email' screen instead of logging in."""
    message: str = "Check your email to finish creating your account."
    verification_required: bool = True


class VerifyEmailRequest(BaseModel):
    """POST /auth/verify-email — redeem the emailed verification link.

    `token` is the short-lived single-use verification JWT (aud=bridgeleads-verify)
    minted by /auth/register. Bounded like the reset token; a JWT is ~hundreds of
    bytes."""
    token: str = Field(max_length=4096)


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
    # Day selectors for recurring schedules. run_at_weekday applies ONLY to
    # frequency="weekly", run_at_day_of_month ONLY to "monthly" (both ignored
    # otherwise). CONTRACT: run_at_weekday is 0=Monday .. 6=Sunday — it matches
    # Python's datetime.weekday(), NOT JS getDay() (the frontend select must map
    # its labels to this, it must not send getDay() raw). Defaults (0=Monday,
    # 1st) reproduce the pre-picker hardcoded behavior, so configs saved before
    # these fields existed keep firing Monday / the 1st with no migration.
    # day_of_month accepts 1..31; the dispatcher clamps to the month's last day,
    # so "31" fires on the last day of short months (Feb -> 28/29).
    run_at_weekday: int = Field(
        default=0, ge=0, le=6,
        description="Weekly only. 0=Monday .. 6=Sunday (matches Python datetime.weekday()). "
                    "Frontend must map its day labels to this; do NOT send JS Date.getDay().",
    )
    run_at_day_of_month: int = Field(
        default=1, ge=1, le=31,
        description="Monthly only. 1..31; the dispatcher clamps to the month's last day, "
                    "so 31 fires on the last day of short months (Feb -> 28/29).",
    )
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

    @model_validator(mode="after")
    def validate_custom_range(self) -> "ScheduleConfig":
        # Reject a backwards custom window at SAVE time (Codex) so the user gets a
        # clear error instead of a silently-wrong scrape. The worker's
        # _ordered_window is the belt for legacy / direct-DB rows; this is the
        # suspenders. Only enforced when mode is custom AND both dates parse —
        # a half-filled form falls back to rolling_90 downstream, not an error.
        if self.date_range_mode == "custom" and self.date_from and self.date_to:
            d0 = _parse_schedule_date(self.date_from)
            d1 = _parse_schedule_date(self.date_to)
            if d0 and d1 and d0 > d1:
                raise ValueError("date_from must be on or before date_to")
        return self


def _parse_schedule_date(value: str) -> date | None:
    """Parse a schedule date string (ISO YYYY-MM-DD or US MM/DD/YYYY) to a date.

    Returns None when it can't be parsed — the caller treats that as "can't
    compare", not as an error, so a malformed string is left to the worker's
    normalizer rather than blocking the save on a format technicality.
    """
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


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
        # Allowlist the formats DataExporter.export() can actually produce.
        # A length-only check let an unsupported value ("pdf", typo) persist
        # on the config and then crash EVERY scrape later at export time with
        # `ValueError: Unsupported export format` (Codex). Reject at save time
        # instead, against the shared SUPPORTED_EXPORT_FORMATS set (case-
        # insensitive). An empty list is normalized to the default so the API
        # readback can't show `[]` while the worker silently exports CSV.
        from src.config.constants import DEFAULT_EXPORT_FORMAT, SUPPORTED_EXPORT_FORMATS
        if not v:
            return [DEFAULT_EXPORT_FORMAT]
        for f in v:
            if len(f) > 16 or f.lower() not in SUPPORTED_EXPORT_FORMATS:
                raise ValueError(f"unsupported export format: {f!r}")
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
    run_at_weekday: int      # 0=Mon..6=Sun, weekly only (see ScheduleConfig)
    run_at_day_of_month: int  # 1..31 (clamped to month length), monthly only
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
    # Phase 3: probate living-owner Transfer-on-Death toggle. None/omitted on a NEW
    # probate config resolves to False (exclude) at the route; True opts in. Only
    # meaningful for record_type=='probate' (route 422s it otherwise).
    include_living_owner_tod: bool | None = None

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
    # Phase 3: None = legacy/grandfathered (include TOD), False = exclude living-owner
    # TOD, True = opt-in. Frontend must NOT echo a default False for a None config (a
    # null read means grandfathered — only a real user toggle should write a value).
    include_living_owner_tod: bool | None = None
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


# Write-only deliver secrets — never returned in GET (redacted to *_set flags in
# ScraperConfigResponse). The edit path keeps a stored secret when the client
# leaves it blank, so these are the only fields a PATCH treats as "preserve on
# omit" instead of "replace".
DELIVER_SECRET_FIELDS: tuple[str, ...] = (
    "webhook_secret",
    "dialer_webhook_secret",
    "phoneburner_access_token",
)


class DeliverUpdate(BaseModel):
    """Deliver payload for PATCH /scrapers/{id} (edit). SHAPE-ONLY: every field is
    lenient here because the route re-validates the merged result by constructing a
    full DeliverConfig (single source of delivery validation). Two reasons this is
    a dedicated model and not DeliverConfig:

    1. Secret preservation — webhook_secret / dialer_webhook_secret /
       phoneburner_access_token are write-only (GET redacts them to *_set bool
       flags). On edit, omitted / null / blank means "keep the stored secret", a
       non-blank value means "replace". DeliverConfig's min-length + cross-field
       (require_connector_credentials) validators would wrongly reject a blank
       "keep" token, so they must NOT run until the route has injected the stored
       secrets back in.
    2. The frontend pre-fills the edit form from GET, whose deliver dict carries
       the write-only readback flags (webhook_secret_set, …). Those are declared
       below (and excluded from model_dump so they never reach DeliverConfig) so
       echoing them back is accepted — while extra="forbid" still rejects any OTHER
       unknown key. That matters because this is a REPLACE-whole payload: a silent
       typo like "webhook_ur" would otherwise drop the real webhook_url + its secret
       (Codex). A misspelled field 422s instead.
    """

    model_config = {"extra": "forbid"}

    emails: list[str] = Field(default_factory=list, max_length=10)
    formats: list[str] = Field(default=["csv"], max_length=5)
    webhook_url: str | None = None
    webhook_secret: str | None = None
    dialer_webhook_url: str | None = None
    dialer_webhook_secret: str | None = None
    dialer_type: str | None = None
    phoneburner_access_token: str | None = None
    phoneburner_owner_id: str | None = None

    # Write-only readback flags emitted by GET. Accepted (so a verbatim form echo
    # doesn't 422) but excluded from model_dump — they are not real deliver fields
    # and must never reach DeliverConfig.
    webhook_secret_set: bool | None = Field(default=None, exclude=True)
    dialer_webhook_secret_set: bool | None = Field(default=None, exclude=True)
    phoneburner_access_token_set: bool | None = Field(default=None, exclude=True)


class ScraperConfigUpdate(BaseModel):
    """PATCH /scrapers/{id} — partial edit of an existing scraper config.

    SEMANTICS: every editable top-level field is optional. A field that is OMITTED
    keeps the stored value; a field that is PRESENT fully REPLACES it (sub-objects
    are replace-whole, NOT deep-merged — the edit wizard pre-fills the complete
    object). The one exception is deliver secrets (see DeliverUpdate): blank = keep.

    Identity (county / state / record_type) is immutable — it ties the config to a
    county_connector and to property_key/dedup/billing history. The fields are
    declared here ONLY so an attempt to change them is rejected with a clear 422
    (Codex P2: reject, don't silently ignore). extra="forbid" rejects any other
    unknown key.

    updated_at is REQUIRED: it is the optimistic-concurrency token (Codex P1). The
    client echoes the value it read from GET; the route 409s if the stored row has
    advanced since, preventing one edit session from silently clobbering another.
    """

    model_config = {"extra": "forbid"}

    updated_at: datetime

    name: str | None = Field(default=None, max_length=120)
    fields: FieldsConfig | None = None
    enrichment: EnrichmentConfig | None = None
    schedule: ScheduleConfig | None = None
    deliver: DeliverUpdate | None = None
    skip_trace_enabled: bool | None = None
    doc_types: list[str] | None = Field(default=None, max_length=10)
    # Phase 3: OMITTED keeps the stored value (an old None config stays grandfathered —
    # editing it must NOT silently flip TOD off); PRESENT replaces it. Only valid for a
    # probate config (route 422s otherwise).
    include_living_owner_tod: bool | None = None

    # Identity — accepted only so the route can 422 if a client tries to change it.
    county: str | None = None
    state: str | None = None
    record_type: str | None = None


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
    # Phase 3: applies ONLY to probate children of this batch (None/omitted => the
    # new probate default, exclude living-owner TOD; True => opt-in). Ignored for
    # non-probate record types in the batch — no 422, since a batch legitimately
    # spans multiple types.
    include_living_owner_tod: bool | None = None

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
    # Phase B: how this county's doc-type selection is enforced and how confident we
    # are. method: "checkbox"/"search_text" (server-side, the portal filters) vs
    # "keyword" (client-side text match after a broad fetch). confidence: "verified"
    # (server-side, live-verified) vs "keyword" (best-effort text match). Lets the UI
    # honestly distinguish a true portal filter from a post-collection text filter.
    # Both None when the county does not support selection. Additive — the checkbox
    # map above is unchanged, so existing clients keep working.
    pre_foreclosure_doc_type_method: str | None = None
    pre_foreclosure_doc_type_confidence: str | None = None
    # SHOW (read-only): what document types / dataset this connector collects, per
    # record type, for the wizard's "documents collected" display. Shape per record
    # type: {kind: "document_type"|"dataset", items: [{label, exact}], note}.
    # Populated by the /connectors handler from each scraper's collection_scope().
    # None when no record type declares a scope. Distinct from the SELECT-capability
    # field pre_foreclosure_doc_types above.
    collection_scope_by_record_type: dict[str, Any] | None = None

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


# ─── Notifications (Phase 2b) ─────────────────────────────────────────────────

class NotificationResponse(BaseModel):
    id: str
    type: NotificationType
    job_id: str | None = None
    detail: dict | None = None
    read_at: datetime | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int


class ReadAllResponse(BaseModel):
    updated: int


# ─── Analytics (Phase 3) ──────────────────────────────────────────────────────

class TrendPoint(BaseModel):
    date: str  # ISO date (YYYY-MM-DD) in ANALYTICS_TIMEZONE
    leads: int


class RecordTypeCount(BaseModel):
    record_type: str  # 'probate' | ... | 'unknown'
    leads: int


class CountyCount(BaseModel):
    county: str  # lowercased county, or 'other' / 'unknown'
    state: str | None  # uppercased 2-letter, None for 'other'/'unknown' buckets
    leads: int


class SkipTraceStats(BaseModel):
    total: int
    enriched: int  # skip_trace_status == 'hit' (trace found contact)
    phone_pct: int  # 0-100, share of total with a primary phone
    email_pct: int  # 0-100, share of total with a primary email


class AnalyticsSummary(BaseModel):
    window_days: int
    timezone: str
    trend: list[TrendPoint]  # dense, zero-filled, today inclusive
    by_record_type: list[RecordTypeCount]
    by_county: list[CountyCount]
    skip_trace: SkipTraceStats
