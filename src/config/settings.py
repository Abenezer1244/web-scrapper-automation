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
    R2_ENDPOINT_URL: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "bridgeleads-exports"
    R2_PUBLIC_URL: str = ""

    # ─── Stripe ───────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_BUSINESS: str = ""
    STRIPE_PRICE_AGENCY: str = ""

    # ─── Email ────────────────────────────────────────────────────────────────
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "leads@bridgeleads.io"

    # ─── App ──────────────────────────────────────────────────────────────────
    DEBUG: bool = False
    ENVIRONMENT: str = "production"
    FRONTEND_URL: str = "https://app.bridgeleads.io"
    ALLOWED_ORIGINS: str = "https://app.bridgeleads.io"

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

    def ensure_dirs(self) -> None:
        self.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.LOGS_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
