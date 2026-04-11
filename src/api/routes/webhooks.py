"""Webhook receivers (Sprint 4).

Currently exposes one endpoint:
    POST /webhooks/tracerfy/{secret}

Tracerfy does not sign requests, so we use a shared-secret URL path as
the auth mechanism. The secret lives in `TRACERFY_WEBHOOK_SECRET` env var
and is configured in the Tracerfy account profile page as part of the
webhook URL, e.g.
    https://api.bridgeleads.io/webhooks/tracerfy/<SECRET>

Tracerfy POSTs a JSON body like:
    {
        "id": 365,                    # tracerfy_queue_id
        "pending": false,
        "download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/.../foo.csv",
        "rows_uploaded": 12,
        "credits_deducted": 12,
        ...
    }

We download the CSV from `download_url`, parse each row, find the
matching `Result` rows by (address, city, state), and upsert phone/email.
"""

import hmac
import secrets as _secrets
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe string comparison to prevent secret length/prefix leaks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@router.post("/tracerfy/{provided_secret}", status_code=status.HTTP_200_OK)
async def tracerfy_webhook(
    provided_secret: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Receive Tracerfy batch completion webhook.

    Auth: constant-time compare of the URL path secret against
    TRACERFY_WEBHOOK_SECRET.

    Processing: queue a background task to download the CSV and ingest
    it, so we return 200 to Tracerfy immediately (their retry semantics
    are unknown, and we don't want to hold the connection open for a
    large CSV parse).
    """
    expected = settings.TRACERFY_WEBHOOK_SECRET
    if not expected:
        _logger.error("Tracerfy webhook hit but TRACERFY_WEBHOOK_SECRET is unset")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Skip trace webhook not configured",
        )

    if not _constant_time_eq(provided_secret, expected):
        # Don't log the provided secret — could be an attack probe
        _logger.warning("Tracerfy webhook received with invalid secret (len=%d)", len(provided_secret))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    try:
        payload = await request.json()
    except Exception as exc:
        _logger.warning("Tracerfy webhook sent invalid JSON: %s", str(exc)[:100])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    queue_id = payload.get("id")
    download_url = payload.get("download_url")
    pending = payload.get("pending", True)

    if queue_id is None or not isinstance(queue_id, int):
        _logger.warning("Tracerfy webhook missing or non-int 'id': %s", payload)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing queue id",
        )

    if pending:
        # Tracerfy should only webhook on completion, but just in case
        _logger.info("Tracerfy webhook queue=%d pending=true — ignoring", queue_id)
        return {"received": True, "pending": True}

    if not download_url:
        _logger.warning("Tracerfy webhook queue=%d missing download_url", queue_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing download_url on completed queue",
        )

    _logger.info(
        "Tracerfy webhook received: queue_id=%d rows=%s credits=%s",
        queue_id,
        payload.get("rows_uploaded"),
        payload.get("credits_deducted"),
    )

    # Offload the actual ingest so Tracerfy gets a fast 200.
    background_tasks.add_task(
        _ingest_webhook_payload,
        queue_id=queue_id,
        download_url=download_url,
        rows_uploaded=payload.get("rows_uploaded", 0),
        credits_deducted=payload.get("credits_deducted", 0),
    )

    return {"received": True, "queue_id": queue_id}


def _ingest_webhook_payload(
    queue_id: int,
    download_url: str,
    rows_uploaded: int,
    credits_deducted: int,
) -> None:
    """Download the completion CSV and upsert phone/email into Result rows.

    Runs as a BackgroundTask after the webhook endpoint returns 200. If
    anything fails here we just log it — Tracerfy won't retry, so the
    failure mode is "skip trace data missing for this batch" which is
    recoverable by re-enqueueing the PendingSkipTraceRow.
    """
    from sqlalchemy import and_, select, update

    from src.db.models import (
        PendingSkipTraceRow,
        Result,
        SkipTraceCache,
        SkipTraceQueue,
    )
    from src.db.session import SyncSessionLocal
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
        return

    try:
        parsed = ingest_webhook_csv(csv_text)
    except Exception as exc:
        _logger.error("Failed to parse CSV for queue %d: %s", queue_id, str(exc)[:200])
        return

    hit_count = 0
    miss_count = 0
    now = datetime.now(UTC)

    with SyncSessionLocal() as db:
        # Load all PendingSkipTraceRow for this queue to match rows back.
        # We match on (lowered street address, city, state) because CSV
        # whitespace may differ slightly from the enqueue-time values.
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
            key = (
                (csv_row["address"] or "").strip().lower(),
                (csv_row["city"] or "").strip().lower(),
                (csv_row["state"] or "").strip().upper(),
            )
            matches = pending_by_key.get(key, [])
            if not matches:
                # Tracerfy normalizes addresses; fuzzy-match on street only
                # as a fallback.
                street_only = key[0]
                for k, v in pending_by_key.items():
                    if k[0] == street_only:
                        matches = v
                        break

            phone = csv_row.get("phone")
            email = csv_row.get("email")
            is_hit = bool(phone or email)

            if is_hit:
                hit_count += 1
            else:
                miss_count += 1

            # Write cache (even misses — so we don't retrace the same
            # address for 90 days just to rediscover it's a dead end)
            cache_key = address_cache_key(
                csv_row["address"], csv_row["city"], csv_row["state"]
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
                        phone_dnc_flag=None,  # DNC scrub is Sprint 5
                        email=email,
                        raw_response=csv_row.get("raw"),
                        fetched_at=now,
                    )
                )

            # Update the matched Result row(s)
            for p in matches:
                db.execute(
                    update(Result)
                    .where(Result.id == p.result_id)
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
                rows_uploaded=rows_uploaded,
                credits_deducted=credits_deducted,
                download_url=download_url,
                completed_at=now,
            )
        )

        # Sprint 4: report metered usage to Stripe. Only over-quota units
        # are billed. Below-quota usage increments a counter without
        # reporting. Any Stripe error is logged and does NOT roll back
        # the ingest — the phone/email data is still saved.
        try:
            from src.api.billing.skip_trace_usage import report_usage_from_webhook
            billing_summary = report_usage_from_webhook(db, queue_id)
            _logger.info("Billing summary for queue %d: %s", queue_id, billing_summary)
        except Exception as exc:
            _logger.error(
                "Billing report failed for queue %d: %s (ingest still committed)",
                queue_id, str(exc)[:200],
            )

        db.commit()

    _logger.info(
        "Tracerfy webhook ingest complete: queue=%d hits=%d misses=%d",
        queue_id, hit_count, miss_count,
    )


def generate_webhook_secret() -> str:
    """Convenience: generate a 32-byte URL-safe secret for TRACERFY_WEBHOOK_SECRET.

    Call via `python -c "from src.api.routes.webhooks import generate_webhook_secret; print(generate_webhook_secret())"`.
    """
    return _secrets.token_urlsafe(32)
