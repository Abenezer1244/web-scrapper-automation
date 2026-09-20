"""Re-raising Celery's time-limit exceptions out of telemetry catch-alls.

``SoftTimeLimitExceeded`` and ``TimeLimitExceeded`` subclass ``Exception``, and
Celery raises them ASYNCHRONOUSLY into whatever line the task happens to be on.
That line is often inside a ``except Exception`` written to make some best-effort
side effect (a log line, a progress observation, a cache refresh) unable to fail
the run. Swallowing the time limit there is not a degraded telemetry write: the
task never learns its budget is gone, skips the cleanup and retry handling the
soft limit exists to trigger, and keeps running until the HARD limit kills the
worker process outright.

So: every catch-all in a best-effort path calls this first.

The check is on the class NAME rather than an ``isinstance`` against an imported
``celery.exceptions``, matching the convention already used in
``src/scrapers/enrichment/`` and ``src/scrapers/king_cv_sources/base.py``. It
keeps the scraper layer free of a Celery import — a scraper is runnable outside a
worker, and the same code path must behave the same either way, where an
``ImportError``-guarded import would silently stop re-raising.
"""

CELERY_TIME_LIMITS = ("SoftTimeLimitExceeded", "TimeLimitExceeded")


def reraise_time_limit(exc: BaseException) -> None:
    """Re-raise ``exc`` when it is a Celery time limit; otherwise do nothing."""
    if type(exc).__name__ in CELERY_TIME_LIMITS:
        raise exc
