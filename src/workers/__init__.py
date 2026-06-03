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
    ],
)

# Upstash Redis uses TLS (rediss://) — kombu needs explicit SSL config.
#
# Why the INT constant here, but the STRING "none" in settings.redis_kwargs():
# the Celery/Kombu broker+backend path uses SYNC redis-py (kombu's redis
# transport builds a redis.SSLConnection; Celery's result backend normalizes
# both forms), and sync redis-py 5.2.1 accepts BOTH ssl.CERT_NONE and "none".
# The direct app client uses redis.asyncio.from_url, whose RedisSSLContext
# crashes on the int ("no attribute cert_reqs") — that's the prod outage
# redis_kwargs() documents, and it does NOT apply to this sync worker path.
# Do not "unify" these to the string without a live-broker smoke test; the int
# is the form proven in production here. (Verified vs kombu 5.4 / redis-py 5.2.1.)
if settings.REDIS_URL.startswith("rediss://"):
    _ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}
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
