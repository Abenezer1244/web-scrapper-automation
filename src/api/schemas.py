from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, field_validator

# ─── Auth ─────────────────────────────────────────────────────────────────────

class UserRegister(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    plan: str
    records_used: int
    records_limit: int
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyResponse(BaseModel):
    api_key: str  # Raw key — shown once, then lost


# ─── Scraper Configs ──────────────────────────────────────────────────────────

class ScheduleConfig(BaseModel):
    frequency: str = "manual"       # manual | daily | weekly | monthly
    time: str = "06:00"             # HH:MM UTC
    range_mode: str = "rolling_90"  # rolling_90 | custom | since_last_run
    date_from: str | None = None
    date_to: str | None = None


class DeliverConfig(BaseModel):
    emails: list[str] = []
    format: str = "csv"             # csv | excel | json
    webhook_url: str | None = None


class ScraperConfigCreate(BaseModel):
    name: str
    county: str
    state: str
    record_type: str
    fields: list[str] = []
    enrichment: list[str] = []
    schedule: ScheduleConfig = ScheduleConfig()
    deliver: DeliverConfig = DeliverConfig()

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
    fields: list[str]
    enrichment: list[str]
    schedule: dict[str, Any]
    deliver: dict[str, Any]
    active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


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
    created_at: datetime

    model_config = {"from_attributes": True}


class ResultsPage(BaseModel):
    job_id: str
    total: int
    page: int
    page_size: int
    items: list[ResultRow]


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

class ConnectorResponse(BaseModel):
    id: str
    county: str
    state: str
    record_types: list[str]
    render_mode: str
    base_url: str
    health_status: str
    last_checked: datetime | None
    active: bool

    model_config = {"from_attributes": True}
