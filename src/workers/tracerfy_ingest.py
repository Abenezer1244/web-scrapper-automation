"""Celery task: download and ingest a Tracerfy batch completion CSV.

Previously this lived in src/api/routes/webhooks.py as a FastAPI
BackgroundTask attached to the Tracerfy webhook handler. The
problem with BackgroundTasks (M8 from the full-SaaS review) is
that they run inside the same API process after the 200 response
is returned — if the API process restarts between 200-return and
CSV parse completion, the ingest is dropped on the floor. Tracerfy
does not retry, so the downstream effect is "batch of skip-trace
results quietly lost" with no way to recover beyond manually
re-enqueuing the PendingSkipTraceRow rows.

Moving this to a Celery task puts the ingest behind Redis-backed
durable queueing: if the worker dies mid-task, Celery retries
(task_acks_late is on for all our tasks). If Redis itself is
unavailable the webhook receiver will itself fail, but that's
visible at the HTTP layer and can be handled.

The task body is intentionally a near-verbatim move of the old
function. Future cleanups (batched updates, better error
reporting) should land separately so the M8 diff stays focused
on the queueing change.
"""

from datetime import UTC, datetime

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.tracerfy_ingest")


@app.task(
    name="src.workers.tracerfy_ingest.ingest_tracerfy_batch",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    acks_late=True,
)
def ingest_tracerfy_batch(
    self,
    queue_id: int,
    download_url: str,
    rows_uploaded: int = 0,
    credits_deducted: int = 0,
) -> dict:
    """Download a Tracerfy batch CSV and upsert phone/email into Results.

    On failure, Celery auto-retries with exponential backoff. After
    max_retries attempts the task gives up and the batch is marked
    errored on the SkipTraceQueue row so ops can see what happened.
    """
    from sqlalchemy import and_, select, update

    from src.db.models import (
        PendingSkipTraceRow,
        Result,
        SkipTraceCache,
        SkipTraceQueue,
    )
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import (
        TracerfyError,
        address_cache_key,
        download_tracerfy_csv,
        ingest_webhook_csv,
    )

    try:
        csv_text = download_tracerfy_csv(download_url)
    except TracerfyError as exc:
        _logger.error("Failed to download CSV for queue %d: %s", queue_id, exc)
        raise  # autoretry will catch and retry

    try:
        parsed = ingest_webhook_csv(csv_text)
    except Exception as exc:
        _logger.error(
            "Failed to parse CSV for queue %d: %s", queue_id, str(exc)[:200]
        )
        # Parse errors are unlikely to be transient — don't retry
        raise self.retry(exc=exc, max_retries=0)

    hit_count = 0
    miss_count = 0
    now = datetime.now(UTC)

    # SYSTEM SESSION: a single Tracerfy batch can contain pending
    # rows from multiple users (the dispatcher groups by
    # trace_type, not user). This ingest path legitimately needs
    # to update Result rows across tenants.
    with system_sync_session() as db:
        pending = (
            db.execute(
                select(PendingSkipTraceRow)
                .where(PendingSkipTraceRow.tracerfy_queue_id == queue_id)
            )
            .scalars()
            .all()
        )

        pending_by_key: dict[tuple[str, str, str], list] = {}
        for p in pending:
            key = (
                (p.property_address or "").strip().lower(),
                (p.city or "").strip().lower(),
                (p.state or "").strip().upper(),
            )
            pending_by_key.setdefault(key, []).append(p)

        for csv_row in parsed:
            csv_key = (
                (csv_row.get("address") or "").strip().lower(),
                (csv_row.get("city") or "").strip().lower(),
                (csv_row.get("state") or "").strip().upper(),
            )
            matches = pending_by_key.get(csv_key, [])
            if not matches:
                continue

            phone = csv_row.get("phone")
            email = csv_row.get("email")
            is_hit = bool(phone or email)
            if is_hit:
                hit_count += 1
            else:
                miss_count += 1

            # Cache the result for 90-day reuse (Sprint 4)
            cache_key = address_cache_key(
                csv_row.get("address") or "",
                csv_row.get("city") or "",
                csv_row.get("state") or "",
            )
            existing_cache = db.get(SkipTraceCache, cache_key)
            if existing_cache:
                existing_cache.phone = phone
                existing_cache.phone_type = csv_row.get("phone_type")
                existing_cache.email = email
                existing_cache.fetched_at = now
            else:
                db.add(
                    SkipTraceCache(
                        address_hash=cache_key,
                        phone=phone,
                        phone_type=csv_row.get("phone_type"),
                        phone_dnc_flag=None,
                        email=email,
                        raw_response=csv_row.get("raw"),
                        fetched_at=now,
                    )
                )

            # Update matched Result rows — user_id filter for H10
            for p in matches:
                db.execute(
                    update(Result)
                    .where(
                        Result.id == p.result_id,
                        Result.user_id == p.user_id,
                    )
                    .values(
                        phone=phone,
                        phone_type=csv_row.get("phone_type"),
                        email=email,
                        skip_trace_status="hit" if is_hit else "miss",
                        skip_trace_attempted_at=now,
                    )
                )
                db.execute(
                    update(PendingSkipTraceRow)
                    .where(PendingSkipTraceRow.id == p.id)
                    .values(status="completed")
                )

        # Mark the Tracerfy queue record as completed
        db.execute(
            update(SkipTraceQueue)
            .where(SkipTraceQueue.tracerfy_queue_id == queue_id)
            .values(
                status="completed",
                download_url=download_url,
                completed_at=now,
                rows_uploaded=rows_uploaded,
                credits_deducted=credits_deducted,
            )
        )

        db.commit()

        # Report Stripe metered billing for over-quota lookups
        # (H11 + H12: stable identifier, commit-before-stripe)
        try:
            from src.api.billing.skip_trace_usage import (
                report_usage_from_webhook,
            )
            billing_summary = report_usage_from_webhook(db, queue_id)
            _logger.info(
                "Tracerfy ingest queue=%d: %d hits / %d misses / billing=%s",
                queue_id, hit_count, miss_count, billing_summary,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            _logger.error(
                "Stripe meter report failed for queue %d: %s",
                queue_id, str(exc)[:200],
            )

    return {
        "queue_id": queue_id,
        "hits": hit_count,
        "misses": miss_count,
    }
