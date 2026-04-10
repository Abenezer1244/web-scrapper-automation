from pathlib import Path
from typing import ClassVar

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ─── Paths ────────────────────────────────────────────────────────────────
    BASE_DIR: ClassVar[Path] = Path(__file__).parent.parent.parent
    DATA_DIR: ClassVar[Path] = BASE_DIR / "data"
    EXPORTS_DIR: ClassVar[Path] = DATA_DIR / "exports"
    LOGS_DIR: ClassVar[Path] = BASE_DIR / "logs"

    # ─── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str
    DATABASE_URL_SYNC: str

    # ─── Redis ────────────────────────────────────────────────────────────────
    REDIS_URL: str

    # ─── Security ─────────────────────────────────────────────────────────────
    SECRET_KEY: str

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_must_be_strong(cls, v: str) -> str:
        insecure = {"changeme", "secret", "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET_KEY_AT_LEAST_32_CHARS"}
        if v in insecure or len(v) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters and not a default value. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    # ─── Cloudflare R2 ────────────────────────────────────────────────────────
    R2_ENDPOINT_URL: str = ""           # Legacy S3-compatible (kept for backwards compat)
    R2_ACCESS_KEY_ID: str = ""          # Legacy S3 access key
    R2_SECRET_ACCESS_KEY: str = ""      # Legacy S3 secret key
    R2_ACCOUNT_ID: str = ""             # Cloudflare account ID
    R2_API_TOKEN: str = ""              # Cloudflare API token with Workers R2 Storage Edit
    R2_BUCKET_NAME: str = "bridgeleads-exports"
    R2_PUBLIC_URL: str = ""

    # ─── Stripe ───────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_BUSINESS: str = ""
    STRIPE_PRICE_AGENCY: str = ""
    STRIPE_PRODUCT_PRO: str = ""
    STRIPE_PRODUCT_BUSINESS: str = ""
    STRIPE_PRODUCT_AGENCY: str = ""

    # ─── Email ────────────────────────────────────────────────────────────────
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "leads@bridgeleads.io"

    # ─── App ──────────────────────────────────────────────────────────────────
    DEBUG: bool = False
    ENVIRONMENT: str = "production"
    FRONTEND_URL: str = "https://bridgeleads.io"
    ALLOWED_ORIGINS: str = "https://bridgeleads.io,https://app.bridgeleads.io,https://bridgeleads-web.vercel.app"

    # ─── Worker scaling ──────────────────────────────────────────────────────
    WORKER_CONCURRENCY: int = 2
    WORKER_QUEUES: str = "scrape,enrichment"

    # ─── Playwright ───────────────────────────────────────────────────────────
    PLAYWRIGHT_HEADLESS: bool = True

    # ─── Scraping ─────────────────────────────────────────────────────────────
    DEFAULT_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    POLITE_DELAY_MS: int = 300

    # ─── AI Extraction (Claude API) ───────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    AI_MODEL: str = "claude-sonnet-4-6"
    AI_MAX_TOKENS: int = 4096
    AI_SCRAPER_ENABLED: bool = False
    AI_COST_ALERT_THRESHOLD: float = 10.0  # USD per day

    # ─── CAPTCHA solving (2Captcha) ───────────────────────────────────────────
    CAPTCHA_API_KEY: str = ""
    CAPTCHA_ENABLED: bool = False

    # ─── Property Data API (Regrid) ───────────────────────────────────────────
    REGRID_API_TOKEN: str = ""
    REGRID_ENABLED: bool = False

    # ─── Free Enrichment (County GIS + AI Assessor) ───────────────────────────
    GIS_ENRICHMENT_ENABLED: bool = True
    AI_ENRICHMENT_ENABLED: bool = True

    # ─── Skip Trace (Tracerfy, Sprint 4) ──────────────────────────────────────
    TRACERFY_API_TOKEN: str = ""
    TRACERFY_API_BASE_URL: str = "https://tracerfy.com"
    TRACERFY_WEBHOOK_SECRET: str = ""
    SKIP_TRACE_ENABLED: bool = False
    SKIP_TRACE_CACHE_DAYS: int = 90
    # Tracerfy rate limit is 10 POSTs per 5-minute window. We leave headroom
    # by only submitting up to 2 batches per dispatcher tick (Beat runs every
    # 5 min). Each batch can hold thousands of rows, so throughput is fine;
    # the constraint is burst count, not total rows.
    SKIP_TRACE_MAX_BATCHES_PER_TICK: int = 2

    @field_validator("TRACERFY_WEBHOOK_SECRET")
    @classmethod
    def webhook_secret_must_be_strong_if_set(cls, v: str) -> str:
        """If a webhook secret is configured, it must be at least 24 chars.
        Empty string is allowed (skip trace disabled)."""
        if v and len(v) < 24:
            raise ValueError(
                "TRACERFY_WEBHOOK_SECRET must be at least 24 characters. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return v

    # ─── Daily Scrape Cache ────────────────────────────────────────────────
    ENABLE_DAILY_SCRAPE: bool = False
    RECORD_RETENTION_DAYS: int = 365

    # ─── Logging ──────────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    # ─── Export ───────────────────────────────────────────────────────────────
    EXPORT_FORMAT: str = "csv"

    # ─── Plan limits: records per month (-1 = unlimited) ──────────────────────
    PLAN_LIMITS: ClassVar[dict[str, int]] = {
        "starter": 50,
        "pro": 500,
        "business": 5000,
        "agency": -1,
    }

    # ─── AI scrape job limits per month (-1 = unlimited) ─────────────────────
    AI_JOB_LIMITS: ClassVar[dict[str, int]] = {
        "starter": 5,
        "pro": 50,
        "business": 500,
        "agency": -1,
    }

    def get_allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    def redis_kwargs(self, decode_responses: bool = True) -> dict:
        """Return kwargs for redis.from_url() with correct SSL config.

        Upstash Redis uses custom TLS certificates not in the system CA store.
        This is the single place where ssl_cert_reqs is configured.
        """
        kwargs: dict = {"decode_responses": decode_responses}
        if self.REDIS_URL.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = "none"
        return kwargs

    def ensure_dirs(self) -> None:
        self.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.LOGS_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
