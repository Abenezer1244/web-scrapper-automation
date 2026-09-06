from pathlib import Path
from typing import ClassVar, Literal

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
    # RLS cutover (Phase 3) target roles:
    #   DATABASE_URL       → bridgeleads_app    (FastAPI request traffic)
    #   DATABASE_URL_SYNC  → bridgeleads_system (Celery workers / scheduler)
    #   DATABASE_URL_MIGRATE → owner/DDL role, Alembic only (optional; falls back
    #     to DATABASE_URL_SYNC pre-cutover — see alembic/env.py). Read directly
    #     by alembic/env.py via os.getenv; declared here for documentation and so
    #     pydantic-settings does not reject it from the environment.
    DATABASE_URL: str
    DATABASE_URL_SYNC: str
    DATABASE_URL_MIGRATE: str = ""

    # ─── Redis ────────────────────────────────────────────────────────────────
    REDIS_URL: str
    # SECURITY (M1): verify the Redis server's TLS certificate by default.
    # Overridable via env ONLY for a deployment whose broker presents a
    # non-publicly-trusted cert. "none" disables MITM protection — avoid.
    REDIS_SSL_CERT_REQS: str = "required"
    REDIS_SSL_CA_CERTS: str = ""  # optional explicit CA bundle path

    # ─── Security ─────────────────────────────────────────────────────────────
    SECRET_KEY: str
    # Field-level encryption key for sensitive DB columns (MFA TOTP secret now;
    # H3 PII later). One or more urlsafe-base64 Fernet keys, comma-separated
    # (first = encrypt, all = decrypt, for rotation). Generate:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # If blank, src/utils/crypto.py derives a key from SECRET_KEY via HKDF
    # (works out-of-the-box; provision a dedicated key to decouple from JWT
    # signing + enable rotation).
    FIELD_ENCRYPTION_KEY: str = ""

    @field_validator("FIELD_ENCRYPTION_KEY")
    @classmethod
    def field_encryption_key_must_be_valid(cls, v: str) -> str:
        """Fail fast at config load on a malformed key, rather than at the first
        encrypt/decrypt call. Blank is allowed (HKDF-from-SECRET_KEY fallback)."""
        raw = (v or "").strip()
        if not raw:
            return v
        from cryptography.fernet import Fernet
        for key in (k.strip() for k in raw.split(",") if k.strip()):
            try:
                Fernet(key)
            except Exception as exc:
                raise ValueError(
                    "FIELD_ENCRYPTION_KEY contains an invalid Fernet key "
                    "(need urlsafe-base64 32-byte key(s), comma-separated). "
                    "Generate: python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\""
                ) from exc
        return v

    # Dedicated, STABLE key for the deterministic HMAC blind index over
    # User.email (H3). MUST be independent of FIELD_ENCRYPTION_KEY: rotating the
    # Fernet key (MultiFernet) must NOT change the blind-index key, or every
    # stored email_hmac stops matching and all logins break. Rotating this key
    # requires a dual-index re-backfill — never rotate it casually. If blank,
    # src/utils/crypto.py derives a key from SECRET_KEY via HKDF (dev fallback;
    # provision a dedicated key in production before strict cutover). Generate:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    BLIND_INDEX_KEY: str = ""

    # H3 PII encryption read mode. While False (tolerant), decrypt_field() treats
    # any value that is not a valid fe1:-prefixed Fernet token as legacy
    # plaintext and returns it as-is — required during the backfill window. Flip
    # to True (strict) ONLY after the backfill verification script reports zero
    # unencrypted in-scope PII rows: strict mode raises on any non-ciphertext,
    # closing the "plaintext silently accepted forever" gap.
    PII_ENCRYPTION_STRICT: bool = False

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
    # Two ways to talk to R2 are supported, in priority order from
    # data_exporter.get_download_url():
    #   1. R2_PUBLIC_URL: if set AND R2_ALLOW_PUBLIC_URLS=true, exports are
    #      returned as direct public URLs (no presigning, no expiry). Exports
    #      contain seller PII, so this is OFF unless BOTH are set — a single
    #      stray R2_PUBLIC_URL must not silently make the bucket world-linkable.
    #   2. R2_ENDPOINT_URL + R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY:
    #      S3-compatible presigning via boto3. THIS IS THE PRODUCTION
    #      PATH today on Railway — do not delete it as "legacy" without
    #      first migrating prod to either path 1 or path 3.
    #   3. R2_ACCOUNT_ID + R2_API_TOKEN: Cloudflare R2 native API
    #      presigning. Reserved for future migration off boto3.
    R2_ENDPOINT_URL: str = ""           # S3-compatible endpoint (production)
    R2_ACCESS_KEY_ID: str = ""          # S3-compatible access key (production)
    R2_SECRET_ACCESS_KEY: str = ""      # S3-compatible secret key (production)
    R2_ACCOUNT_ID: str = ""             # Cloudflare account ID (R2 native API)
    R2_API_TOKEN: str = ""              # Cloudflare API token with Workers R2 Storage Edit
    R2_BUCKET_NAME: str = "bridgeleads-exports"
    R2_PUBLIC_URL: str = ""
    # Explicit opt-in for the public-URL path above. Default False so exports
    # stay behind presigned/streamed URLs (PII). Only set true if the bucket is
    # deliberately public-read AND that's acceptable for the data.
    R2_ALLOW_PUBLIC_URLS: bool = False

    # ─── Stripe ───────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_BUSINESS: str = ""
    STRIPE_PRICE_AGENCY: str = ""
    # Annual (yearly-interval) Stripe Price IDs — wired into checkout so the
    # ~20%-off annual prepay shown on /plans is actually buyable, not display-only.
    STRIPE_PRICE_PRO_ANNUAL: str = ""
    STRIPE_PRICE_BUSINESS_ANNUAL: str = ""
    STRIPE_PRICE_AGENCY_ANNUAL: str = ""
    STRIPE_PRODUCT_PRO: str = ""
    STRIPE_PRODUCT_BUSINESS: str = ""
    STRIPE_PRODUCT_AGENCY: str = ""
    # Sprint 4: Stripe metered billing for skip-trace lookups
    STRIPE_PRODUCT_SKIP_TRACE: str = ""
    STRIPE_METER_SKIP_TRACE: str = ""
    STRIPE_METER_EVENT_NAME_SKIP_TRACE: str = "skip_trace_lookup"
    STRIPE_PRICE_SKIP_TRACE_PRO: str = ""
    STRIPE_PRICE_SKIP_TRACE_BUSINESS_OVERAGE: str = ""
    STRIPE_PRICE_SKIP_TRACE_AGENCY_OVERAGE: str = ""
    # Bundled monthly quotas by plan — the webhook ingest reports usage
    # only for lookups BEYOND these quotas. Below the quota, the user is
    # not billed per-trace (the cost is absorbed into the base plan price).
    SKIP_TRACE_BUNDLED_QUOTAS: ClassVar[dict[str, int]] = {
        "starter": 0,
        "pro": 250,        # 250 included/month (2026-06 strategy), then $0.08/trace
        "business": 1000,  # Business gets 1000 free/month, then $0.08/trace
        "agency": 2000,    # Agency gets 2000 free/month, then $0.05/trace
    }

    # Per-tier entitlement enforcement (value-metric: county access + record-type
    # gating — see src/api/entitlements.py + src/config/constants.py). Default
    # FALSE = audit/log-only: the validator computes would-block violations and
    # logs them but does NOT 402, so we ship the infrastructure + measure impact
    # without reversing the "all paid plans access all counties" marketing or
    # locking out existing accounts. Flip per-service (api+worker) ONLY after the
    # pricing/UI/copy land and existing accounts are intentionally grandfathered.
    ENTITLEMENT_ENFORCEMENT: bool = False

    # ─── Email ────────────────────────────────────────────────────────────────
    RESEND_API_KEY: str = ""
    # The VERIFIED Resend sending address. Changing it breaks SPF/DKIM/DMARC
    # alignment and provider verification — do not edit without re-verifying the
    # domain in Resend.
    EMAIL_FROM: str = "leads@bridgeleads.io"
    # Display name recipients see in their inbox. Without it the From header is a
    # bare address and Gmail falls back to rendering the local part ("leads"),
    # which is what shipped. Composed into the From header by
    # src/utils/email_layout.from_header() — the ONLY place that builds it.
    EMAIL_FROM_NAME: str = "BridgeLeads"
    # Where a recipient's reply lands. Separate from EMAIL_FROM on purpose: the
    # From address is the deliverability-verified sender, this is the human
    # inbox our footers point at. Empty = omit the Reply-To header entirely
    # (replies then go to EMAIL_FROM).
    EMAIL_REPLY_TO: str = "support@bridgeleads.io"
    # Support address printed in email footers / help copy.
    SUPPORT_EMAIL: str = "support@bridgeleads.io"

    # Registration email-verification flow (anti-enumeration). Default FALSE =
    # legacy behavior: /auth/register creates the account immediately and returns
    # session tokens (201), so a deploy of this backend is a NO-OP until the flag
    # is flipped. When TRUE, register is enumeration-safe: it returns the SAME
    # neutral 200 for a new vs existing email, stores the signup in
    # pending_registrations (NOT a real users row — closes account-squatting),
    # and the account is created only when the emailed verification link is
    # redeemed at /auth/verify-email. Flip ONLY after the frontend signup +
    # verify pages ship (the new flow returns no tokens, so the old frontend
    # would break on signup with the flag on).
    EMAIL_VERIFICATION_ENABLED: bool = False

    # ─── App ──────────────────────────────────────────────────────────────────
    DEBUG: bool = False
    ENVIRONMENT: str = "production"
    # RLS enforcement gate (REDTEAM T2). Default False = advisory-only: startup
    # LOGS if the DB role bypasses RLS but still boots (today's behavior — the
    # production role currently has BYPASSRLS and worker paths depend on it).
    # Set True ONLY after the staged RLS cutover (non-BYPASSRLS role +
    # per-transaction GUC reapply + a system RLS policy for system_sync_session,
    # the deferred HIGH-2 work); with it on, the API + workers REFUSE to boot on
    # a bypassing role. Enabling it before the cutover WILL break scrapes/ingest.
    RLS_ENFORCE: bool = False
    FRONTEND_URL: str = "https://bridgeleads.io"
    # Public base URL of THIS API (the host serving /jobs/{id}/download).
    # When set, delivery emails/webhooks send a revocable app download-token
    # link instead of a raw 48h R2 presigned URL. Empty = fall back to the
    # presigned URL (so delivery keeps working until this is configured).
    API_BASE_URL: str = ""
    ALLOWED_ORIGINS: str = "https://bridgeleads.io,https://app.bridgeleads.io,https://bridgeleads-web.vercel.app"
    # Number of trusted reverse-proxy hops in front of the API (each appends one
    # entry to X-Forwarded-For). Railway/Fly = 1. Used by the rate limiter to
    # take the real client IP from the RIGHT of the XFF chain (rate_limit I1).
    TRUSTED_PROXY_HOPS: int = 1

    # ─── Analytics (Phase 3) ──────────────────────────────────────────────────
    # Day-grouping timezone for the dashboard charts. Postgres groups
    # `date(created_at)` in the session TZ (UTC on Supabase), which would split
    # a Pacific user's day at the wrong boundary. The analytics endpoint groups
    # by created_at AT TIME ZONE this value. Must be a valid IANA zone name.
    ANALYTICS_TIMEZONE: str = "America/Los_Angeles"

    # ─── Worker scaling ──────────────────────────────────────────────────────
    WORKER_CONCURRENCY: int = 2
    WORKER_QUEUES: str = "scrape,enrichment"

    # ─── Playwright ───────────────────────────────────────────────────────────
    PLAYWRIGHT_HEADLESS: bool = True

    # Which identity the browser presents to portals. See
    # src/scrapers/browser_identity.py for why this is flagged rather than
    # simply corrected: changing the UA can change how a portal responds
    # (content negotiation, bot rules, redirects, cookie gates), and scraper
    # navigation has no unit coverage. Default reproduces the previous
    # hardcoded string byte-for-byte, so shipping this is a no-op.
    #   legacy          — previous hardcoded Windows/Chrome-120 string
    #   linux_dynamic   — real Chromium major + Linux (coherent with the
    #                     container and navigator.platform; the intended target)
    #   windows_dynamic — real major but keeps the Windows claim; for canary
    #                     comparison only
    # Literal, not str: a typo like "linux-dynamic" would otherwise fall through
    # to the legacy UA at the call site and silently turn a canary into a no-op,
    # invalidating the rollout data. pydantic-settings rejects it at boot instead.
    SCRAPER_BROWSER_UA_MODE: Literal["legacy", "linux_dynamic", "windows_dynamic"] = "legacy"
    # Operator escape hatch — wins over the mode. Empty = unused.
    SCRAPER_BROWSER_UA_OVERRIDE: str = ""

    # ─── Scraping ─────────────────────────────────────────────────────────────
    DEFAULT_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    POLITE_DELAY_MS: int = 300
    # Hard ceiling for a single server-side file download (safe_download_to_file).
    # Bounds memory/disk/network DoS from an oversized or hostile response — the
    # worker runs ~512 MB alongside Chromium, and several jobs can stream at once.
    # Default 100 MB: ~2x the largest known county bulk file (Snohomish tax list
    # ~45 MB) with growth headroom. A response exceeding this aborts and raises.
    MAX_DOWNLOAD_BYTES: int = 104857600  # 100 * 1024 * 1024

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

    # ─── M6: operational alerting (watchdog / canary / batch failures) ────────
    # Empty = disabled (dev/CI default). Set to an ops inbox in prod; alerts go
    # out via the existing Resend integration. Cooldown dedupes repeats per
    # (kind, key) so a down portal emails once per window, not once per tick.
    OPS_ALERT_EMAIL: str = ""
    OPS_ALERT_COOLDOWN_SECONDS: int = 21600  # 6h

    # ─── Plan limits: records per month (-1 = unlimited) ──────────────────────
    PLAN_LIMITS: ClassVar[dict[str, int]] = {
        "starter": 50,
        "pro": 1000,
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
        # I4: CORS runs with allow_credentials=True, so a wildcard or plaintext
        # origin would let any site (or a MITM on http) make credentialed
        # cross-origin calls. Accept ONLY explicit https:// origins; permit
        # http://localhost / 127.0.0.1 for local dev. A stray "*" or arbitrary
        # http:// entry is dropped rather than silently widening the allowlist.
        out: list[str] = []
        for raw in self.ALLOWED_ORIGINS.split(","):
            origin = raw.strip()
            if not origin or origin == "*":
                continue
            if origin.startswith("https://") or origin.startswith(
                ("http://localhost", "http://127.0.0.1")
            ):
                out.append(origin)
        return out

    def redis_kwargs(self, decode_responses: bool = True) -> dict:
        """Return kwargs for redis.from_url() with correct SSL config.

        SECURITY (M1): for rediss:// we VERIFY the server certificate. The
        broker holds the JWT blacklist + brute-force/rate-limit counters, so a
        MITM on the Redis link could undermine auth — cert verification closes
        that. Upstash presents a publicly-trusted certificate (the billing.py
        rediss:// clients already connect with redis-py's default "required"
        and work in prod), so we pin ssl_cert_reqs="required" and point at
        certifi's CA bundle so validation succeeds regardless of the
        container's system CA store. REDIS_SSL_CERT_REQS is an env-only escape
        hatch (set "none") in case a future broker uses a private cert chain.

        NOTE: redis-py's SSL context builder accepts the STRING
        "none"/"optional"/"required" here — NOT the ssl.CERT_NONE integer
        constant, which crashes at connect with "RedisSSLContext object has no
        attribute cert_reqs" (the L5 prod outage). Keep these as strings.
        """
        kwargs: dict = {"decode_responses": decode_responses}
        if self.REDIS_URL.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = self.REDIS_SSL_CERT_REQS
            # Only attach a CA bundle when actually verifying.
            if self.REDIS_SSL_CERT_REQS != "none":
                ca = self.REDIS_SSL_CA_CERTS
                if not ca:
                    try:
                        import certifi
                        ca = certifi.where()
                    except Exception:
                        ca = ""
                if ca:
                    kwargs["ssl_ca_certs"] = ca
        return kwargs

    def ensure_dirs(self) -> None:
        self.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.LOGS_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
