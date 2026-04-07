import ssl

from celery import Celery
from kombu import Exchange, Queue

from src.config import settings

app = Celery(
    "bridgeleads",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["src.workers.tasks", "src.workers.scheduler"],
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
