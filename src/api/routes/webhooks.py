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

from fastapi import APIRouter, HTTPException, Request, status

from src.api.middleware import rate_limit
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
) -> dict:
    """Receive Tracerfy batch completion webhook.

    Auth: constant-time compare of the URL path secret against
    TRACERFY_WEBHOOK_SECRET.

    Processing: queue a background task to download the CSV and ingest
    it, so we return 200 to Tracerfy immediately (their retry semantics
    are unknown, and we don't want to hold the connection open for a
    large CSV parse).
    """
    # C5 (full-SaaS review): rate-limit BEFORE the secret comparison
    # so a brute-force loop hitting invalid secrets gets 429'd
    # quickly. Legitimate Tracerfy deliveries fire once per batch
    # completion — minutes apart — so the 120/min cap is far above
    # any normal traffic.
    await rate_limit(request, zone="webhook")

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

    # REDTEAM B1: edge dedup. Drop duplicate inbound deliveries of the same
    # completed batch BEFORE we dispatch a Celery task, mirroring the Stripe
    # webhook's SET NX EX idempotency in billing.py. The worker also has a
    # DB-level replay guard (locks the SkipTraceQueue row and no-ops once it
    # is "completed"), but this Redis claim is the cheap first line of
    # defense: two near-simultaneous retries can't both enqueue an ingest.
    # set(..., nx=True, ex=N) returns True on first write, None on conflict.
    # TTL covers Tracerfy's redelivery window with margin. If Redis is
    # unavailable we do NOT block the delivery — the worker's DB guard still
    # makes re-processing idempotent — so a Redis error falls through to
    # dispatch rather than dropping the batch.
    import redis.asyncio as aioredis
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        dedup_key = f"tracerfy_webhook:{queue_id}"
        claimed = await redis.set(dedup_key, "1", nx=True, ex=86400)  # 1 day
        if not claimed:
            _logger.info(
                "Tracerfy webhook dedup: already dispatched queue_id=%d", queue_id
            )
            return {"received": True, "queue_id": queue_id, "deduped": True}
    except Exception as exc:  # noqa: BLE001 — Redis down must not drop the batch
        _logger.warning(
            "Tracerfy webhook dedup check failed (queue_id=%d): %s — "
            "falling through to dispatch; worker DB guard stays idempotent",
            queue_id, str(exc)[:120],
        )
    finally:
        try:
            await redis.aclose()
        except Exception:  # noqa: BLE001 — best-effort close
            pass

    # M8 (full-SaaS review): dispatch to Celery instead of
    # FastAPI BackgroundTasks. The previous BackgroundTask ran
    # inside the API process after the 200 was returned — if the
    # API process restarted before CSV parsing completed, the
    # batch was dropped on the floor because Tracerfy does NOT
    # retry webhooks. Celery persists the task in Redis and retries
    # automatically if the worker dies mid-task.
    from src.workers.tracerfy_ingest import ingest_tracerfy_batch
    ingest_tracerfy_batch.delay(
        queue_id=queue_id,
        download_url=download_url,
        rows_uploaded=payload.get("rows_uploaded", 0),
        credits_deducted=payload.get("credits_deducted", 0),
    )

    return {"received": True, "queue_id": queue_id}



def generate_webhook_secret() -> str:
    """Convenience: generate a 32-byte URL-safe secret for TRACERFY_WEBHOOK_SECRET.

    Call via `python -c "from src.api.routes.webhooks import generate_webhook_secret; print(generate_webhook_secret())"`.
    """
    return _secrets.token_urlsafe(32)
