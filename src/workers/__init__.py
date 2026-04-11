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
    ],
)

# Upstash Redis uses TLS (rediss://) — kombu needs explicit SSL config.
# See settings.redis_kwargs() for the single-source SSL policy.
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
    "src.workers.tasks.enrich_job_results": {"queue": "enrichment"},
    "src.workers.scheduler.*": {"queue": "celery"},
    "src.workers.skip_trace_dispatcher.*": {"queue": "celery"},
    "src.workers.webhook_delivery.*": {"queue": "celery"},
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
    except Exception as exc:  # noqa: BLE001 — defensive
        logging.getLogger("worker.bootstrap").warning(
            "RLS advisory check skipped at worker boot: %s", exc
        )
