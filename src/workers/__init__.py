from celery import Celery

from src.config import settings

app = Celery(
    "bridgeleads",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["src.workers.tasks", "src.workers.scheduler"],
)

app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=1700,   # 28 min — warn worker before hard kill
    task_time_limit=1800,        # 30 min — hard kill
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
