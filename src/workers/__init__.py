import ssl

from celery import Celery
from kombu import Exchange, Queue

from src.config import settings

app = Celery(
    "bridgeleads",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "src.workers.tasks",
        "src.workers.scheduler",
        "src.workers.skip_trace_dispatcher",
        "src.workers.webhook_delivery",
        "src.workers.tracerfy_ingest",
        # Piece 2 batch scrape: the worker MUST import this or `dispatch_batch_run`
        # (enqueued by POST /batches) is an unregistered task and gets dropped —
        # the batch then sits at `pending` forever (caught in prod E2E test).
        "src.workers.batch_tasks",
        # NTS Tier 1: the beat crawler that fills nts_notices (trustee-sale auction
        # data). Must be imported here or the scheduled task is unregistered.
        "src.workers.nts_crawler",
        # NTS Tier 1: the matcher beat that attaches auction data onto leads.
        "src.workers.nts_matcher_task",
        # Duplicate-signup notice: send_duplicate_signup_email is .delay()-ed from
        # POST /auth/register. Must be imported here or the task is unregistered
        # and the enqueue is silently dropped.
        "src.workers.onboarding_emails",
        # Lead delivery email: deliver_job_email is .delay()-ed from run_scrape_job
        # and the batch finalizer. Must be imported here or the task is
        # unregistered and the enqueue is silently dropped (same trap as above).
        "src.workers.delivery",
    ],
)

# Upstash Redis uses TLS (rediss://) — kombu needs explicit SSL config.
#
# SECURITY (M1): VERIFY the broker certificate. This transport carries every
# task payload (scrape results, skip-trace data, dialer pushes), so an
# unverified TLS link is a MITM surface. Upstash presents a publicly-trusted
# cert, so CERT_REQUIRED + certifi's CA bundle validates it. Mirrors
# settings.redis_kwargs() and honors the same REDIS_SSL_CERT_REQS escape hatch.
#
# Why the ssl.* INT constants here, but the STRING form in redis_kwargs(): the
# Celery/Kombu broker+backend path uses SYNC redis-py (kombu builds a
# redis.SSLConnection) which takes the ssl module constants. The direct app
# client uses redis.asyncio.from_url, whose RedisSSLContext crashes on the int
# ("no attribute cert_reqs") and needs the string — that asymmetry is real, do
# not unify the two forms. (kombu 5.4 / redis-py 5.2.1.)
if settings.REDIS_URL.startswith("rediss://"):
    try:
        import certifi
        _ca_default = certifi.where()
    except Exception:
        _ca_default = ""
    if settings.REDIS_SSL_CERT_REQS == "none":
        import logging
        logging.getLogger("worker.bootstrap").warning(
            "REDIS broker TLS cert verification is DISABLED "
            "(REDIS_SSL_CERT_REQS=none) — MITM protection off on the broker."
        )
        _ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}
    else:
        _ssl_opts = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        _ca = settings.REDIS_SSL_CA_CERTS or _ca_default
        if _ca:
            _ssl_opts["ssl_ca_certs"] = _ca
    app.conf.broker_use_ssl = _ssl_opts
    app.conf.redis_backend_use_ssl = _ssl_opts

# ─── Task queues ─────────────────────────────────────────────────────────────
# Separate queues let you scale scrape workers and enrichment workers
# independently. "celery" is the default queue for scheduler/beat tasks.
_default = Exchange("default", type="direct")

app.conf.task_queues = (
    Queue("celery", _default, routing_key="celery"),
    Queue("scrape-priority", _default, routing_key="scrape-priority"),
    Queue("scrape", _default, routing_key="scrape"),
    Queue("enrichment", _default, routing_key="enrichment"),
)
app.conf.task_default_queue = "celery"

# Route tasks to their queues (default routing — can be overridden per-call)
app.conf.task_routes = {
    "src.workers.tasks.run_scrape_job": {"queue": "scrape"},
    "src.workers.scheduler.*": {"queue": "celery"},
    "src.workers.skip_trace_dispatcher.*": {"queue": "celery"},
    "src.workers.webhook_delivery.*": {"queue": "celery"},
    "src.workers.tracerfy_ingest.*": {"queue": "enrichment"},
}

app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=3300,   # 55 min — warn worker before hard kill
    task_time_limit=3600,        # 60 min — hard kill
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # H13 (full-SaaS review): recycle worker processes after every
    # 25 completed tasks. Playwright sometimes leaves orphaned
    # Chromium processes when a task is hard-killed at time_limit,
    # or when one of the close() calls in base_scraper.__aexit__
    # fails silently. Recycling the worker process reclaims those
    # file handles + RAM and prevents a slow memory creep that
    # ends in OOM on long-running Railway deploys.
    worker_max_tasks_per_child=25,
)


# ─── SSRF allowlist bootstrap ────────────────────────────────────────────────
# Load every active county connector's base_url hostname into the
# SSRF allowlist when a worker boots. Connectors seeded via Alembic
# migration or scripts never pass through the API route that calls
# validate_scraping_target(), so without this hook the scrape worker
# would throw "Scraping target not in approved domain list" on the
# first scrape of any un-template-matched AI connector. See Sprint
# 6.3 Phase 3 audit in docs/compliance/connector-audit-2026-04-10.md
from celery.signals import worker_ready  # noqa: E402


@worker_ready.connect
def _bootstrap_ssrf_allowlist(sender=None, **_kwargs) -> None:
    """Register all active connector domains with the SSRF allowlist.

    Runs once per worker process at startup. Failures are logged and
    swallowed so a transient DB issue does not block worker boot.
    Also runs the RLS advisory check so worker logs make the
    multi-tenant isolation posture visible alongside the API logs.
    """
    import logging

    # BACKLOG §4: API_BASE_URL is required in production so delivery emails mint
    # revocable app download-token links instead of the broken R2/S3 presign
    # fallback (which 401s). _delivery_download_url() hard-fails the delivery if
    # it is missing; surface the misconfig at BOOT — before the first delivery
    # job — so env drift is caught immediately rather than per-job.
    if settings.ENVIRONMENT.strip().lower() == "production" and not settings.API_BASE_URL:
        logging.getLogger("worker.bootstrap").error(
            "API_BASE_URL is UNSET in production — delivery emails/webhooks "
            "will FAIL (the R2/S3 presign fallback is broken). Set API_BASE_URL "
            "on the worker service."
        )

    # Fail fast at boot if field encryption is misconfigured (production/strict with
    # no FIELD_ENCRYPTION_KEY): build the Fernet now so a missing key breaks worker
    # startup rather than silently encrypting PII under the SECRET_KEY-derived
    # fallback (incident 2026-06 stranded 61 users.email). _build_fernet() raises.
    from src.utils.crypto import _instance
    _instance()

    try:
        from src.api.middleware import register_connector_domains_from_db
        register_connector_domains_from_db()
    except Exception as exc:  # noqa: BLE001 — defensive
        logging.getLogger("worker.bootstrap").warning(
            "Connector domain registration skipped at worker boot: %s", exc
        )
    try:
        from src.db.session import check_rls_role_status
        check_rls_role_status()
    except RuntimeError:
        # REDTEAM HIGH T2 (Codex convergence P1): production fail-closed. A
        # BYPASSRLS/superuser runtime role makes the 025 WITH CHECK policies
        # (and all RLS) inert. check_rls_role_status raises ONLY in that
        # production case — let it propagate so the WORKER refuses to boot too
        # (the API lifespan already does), instead of silently running
        # tenant-unsafe with RLS effectively off. The previous bare
        # `except Exception` swallowed exactly this guard.
        raise
    except Exception as exc:  # noqa: BLE001 — other (advisory) errors must not wedge boot
        logging.getLogger("worker.bootstrap").warning(
            "RLS advisory check skipped at worker boot: %s", exc
        )

    # RLS cutover Phase 2b: warm the public sample cache so /scrapers/sample is
    # not empty after a deploy (it now reads the precomputed public_sample_cache
    # instead of live-querying tenant tables). Best-effort enqueue — the task is
    # an idempotent singleton upsert, so duplicate runs across workers are
    # harmless. The hourly beat task keeps it fresh thereafter.
    try:
        from src.workers.scheduler import refresh_public_sample_cache
        refresh_public_sample_cache.delay()
    except Exception as exc:  # noqa: BLE001 — never wedge boot on a cache warm
        logging.getLogger("worker.bootstrap").warning(
            "public_sample_cache warm enqueue skipped at worker boot: %s", exc
        )
