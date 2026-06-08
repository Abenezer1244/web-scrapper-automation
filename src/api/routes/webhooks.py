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


# Preferred auth: the secret travels in this header, NOT the URL path (a path
# secret leaks into access logs / Referer — Codex security cross-check). The
# legacy path route below stays until Tracerfy is reconfigured to this header.
_SECRET_HEADER = "X-Tracerfy-Webhook-Secret"


def _verify_tracerfy_secret(provided: str | None) -> None:
    """Constant-time check of a provided secret against TRACERFY_WEBHOOK_SECRET.

    Raises 503 if the server has no secret configured, 401 if the provided
    secret is missing or wrong. Never logs the provided value.
    """
    expected = settings.TRACERFY_WEBHOOK_SECRET
    if not expected:
        _logger.error("Tracerfy webhook hit but TRACERFY_WEBHOOK_SECRET is unset")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Skip trace webhook not configured",
        )
    if not provided or not _constant_time_eq(provided, expected):
        _logger.warning(
            "Tracerfy webhook received with invalid secret (len=%d)", len(provided or "")
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


async def _process_tracerfy_webhook(request: Request) -> dict:
    """Parse + validate the Tracerfy body and dispatch ingest. Auth is the
    caller's responsibility (done before this is invoked)."""
    try:
        payload = await request.json()
    except Exception as exc:
        _logger.warning("Tracerfy webhook sent invalid JSON: %s", str(exc)[:100])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    # A non-object JSON body (array/string/number) would 500 on payload.get(...)
    # below — reject it as a 400 first (Codex M2 review).
    if not isinstance(payload, dict):
        _logger.warning(
            "Tracerfy webhook body is %s, not a JSON object", type(payload).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    queue_id = payload.get("id")
    download_url = payload.get("download_url")
    pending = payload.get("pending", True)

    if queue_id is None or not isinstance(queue_id, int):
        # Log only the payload KEYS, never the full body — it can carry a
        # signed download_url whose token is a secret (M2).
        _logger.warning(
            "Tracerfy webhook missing or non-int 'id'; payload keys=%s",
            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
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

    # Dispatch the ingest to Celery (M8: durable Redis-backed queue — a
    # FastAPI BackgroundTask would be lost if the API restarted before CSV
    # parsing finished, and Tracerfy does NOT reliably retry webhooks).
    #
    # No edge dedup here (Codex review): idempotency is owned authoritatively by
    # the worker, which locks the SkipTraceQueue row (SELECT ... FOR UPDATE) and
    # no-ops once it is completed/billed, while billing is made durable by the
    # meter outbox. An earlier Redis SET-NX edge claim was REMOVED because
    # claiming the key BEFORE the worker validated the delivery let a
    # forged/malformed first webhook for a real queue_id suppress the genuine
    # retry for the whole TTL — net-negative versus the worker's DB guard.
    from src.workers.tracerfy_ingest import ingest_tracerfy_batch
    ingest_tracerfy_batch.delay(
        queue_id=queue_id,
        download_url=download_url,
        rows_uploaded=payload.get("rows_uploaded", 0),
        credits_deducted=payload.get("credits_deducted", 0),
    )

    return {"received": True, "queue_id": queue_id}


@router.post("/tracerfy", status_code=status.HTTP_200_OK)
async def tracerfy_webhook(request: Request) -> dict:
    """Receive Tracerfy batch completion webhook (PREFERRED).

    Auth: constant-time compare of the `X-Tracerfy-Webhook-Secret` header against
    TRACERFY_WEBHOOK_SECRET — keeps the secret out of the URL (and out of access
    logs / Referer). Configure Tracerfy to POST here with that header.
    """
    # Rate-limit BEFORE the secret compare so a brute-force loop hitting invalid
    # secrets is 429'd quickly (legitimate deliveries fire minutes apart).
    await rate_limit(request, zone="webhook")
    _verify_tracerfy_secret(request.headers.get(_SECRET_HEADER))
    return await _process_tracerfy_webhook(request)


@router.post("/tracerfy/{provided_secret}", status_code=status.HTTP_200_OK)
async def tracerfy_webhook_legacy(provided_secret: str, request: Request) -> dict:
    """LEGACY: secret in the URL path. Deprecated — the path secret leaks into
    access logs. Migrate Tracerfy to `POST /webhooks/tracerfy` with the
    `X-Tracerfy-Webhook-Secret` header, rotate TRACERFY_WEBHOOK_SECRET, then this
    route can be removed.

    Header-first (Codex): if the header is present it is authoritative — a wrong
    header is rejected even if the path secret is right. Current Tracerfy traffic
    sends no header, so this branch is inert until migration.
    """
    await rate_limit(request, zone="webhook")
    header_secret = request.headers.get(_SECRET_HEADER)
    _verify_tracerfy_secret(header_secret if header_secret is not None else provided_secret)
    return await _process_tracerfy_webhook(request)


def generate_webhook_secret() -> str:
    """Convenience: generate a 32-byte URL-safe secret for TRACERFY_WEBHOOK_SECRET.

    Call via `python -c "from src.api.routes.webhooks import generate_webhook_secret; print(generate_webhook_secret())"`.
    """
    return _secrets.token_urlsafe(32)
