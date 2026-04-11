"""Sprint 6.5: outbound webhook delivery on job completion.

Business+ plan feature (gated upstream in scrapers route). When a
scrape job completes, if the scraper_config has a `deliver.webhook_url`
set, this Celery task POSTs a JSON summary to that URL with a 48-hour
signed download link for the CSV export.

Retries up to 3 times with Celery's built-in exponential backoff
(~1s, 5s, 25s) on any non-2xx response or network error.

Payload shape (stable — consumers rely on these keys):
    {
      "event": "job.completed",
      "delivered_at": "2026-04-11T03:45:22.123456+00:00",
      "job": {
        "id": "<uuid>",
        "scraper_config_id": "<uuid>",
        "status": "done",
        "record_count": 126,
        "started_at": "2026-04-11T03:42:00+00:00",
        "finished_at": "2026-04-11T03:45:18+00:00"
      },
      "scraper": {
        "id": "<uuid>",
        "name": "Pierce Probate Weekly",
        "county": "pierce",
        "state": "WA",
        "record_type": "probate"
      },
      "download": {
        "format": "csv",
        "url": "<signed R2 URL, 48h expiry>",
        "expires_at": "2026-04-13T03:45:22+00:00"
      },
      "signature": "<hex HMAC-SHA256 over payload, using deliver.webhook_secret if set>"
    }

Optional HMAC signature:
    If scraper_configs.deliver.webhook_secret is set, the `signature`
    field contains HMAC-SHA256(secret, canonical_json_payload) so
    consumers can verify authenticity. If no secret is set, signature
    is an empty string.
"""
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import requests

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.webhook_delivery")

# Retry policy: 3 attempts total with exponential backoff
_MAX_RETRIES = 3
_BACKOFF_BASE = 5  # seconds — actual waits: 5s, 25s, 125s

# Webhook target timeout — enough for slow Zapier/Make cold starts
_HTTP_TIMEOUT = 15


def _sign_payload(payload: dict, secret: str | None) -> str:
    """Return hex HMAC-SHA256 of canonicalized JSON payload. Empty if no secret."""
    if not secret:
        return ""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_webhook_payload(
    job_id: str,
    scraper_config_id: str,
    scraper_name: str,
    county: str,
    state: str,
    record_type: str,
    status: str,
    record_count: int,
    started_at,
    finished_at,
    export_key: str | None,
    fmt: str,
    download_url: str | None,
    webhook_secret: str | None,
) -> dict:
    """Build the canonical webhook payload. Public for unit verification."""
    now = datetime.now(UTC)
    expires_at = (now + timedelta(hours=48)).isoformat() if download_url else None

    payload = {
        "event": "job.completed",
        "delivered_at": now.isoformat(),
        "job": {
            "id": job_id,
            "scraper_config_id": scraper_config_id,
            "status": status,
            "record_count": record_count,
            "started_at": started_at.isoformat() if started_at else None,
            "finished_at": finished_at.isoformat() if finished_at else None,
        },
        "scraper": {
            "id": scraper_config_id,
            "name": scraper_name,
            "county": county,
            "state": state,
            "record_type": record_type,
        },
        "download": {
            "format": fmt,
            "url": download_url,
            "expires_at": expires_at,
            "export_key": export_key,
        },
    }
    payload["signature"] = _sign_payload(payload, webhook_secret)
    return payload


@app.task(
    name="src.workers.webhook_delivery.deliver_job_webhook",
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_BACKOFF_BASE,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def deliver_job_webhook(self, job_id: str, webhook_url: str, payload: dict) -> dict:
    """POST a job-completion payload to the configured webhook URL.

    Args:
        job_id: The job UUID (for logging correlation only)
        webhook_url: Full URL to POST to
        payload: Pre-built JSON payload (from build_webhook_payload)

    Returns:
        Dict with {status_code, response_excerpt, attempts}.

    Raises:
        requests.RequestException: on network error (Celery auto-retries)
        Exception: on non-2xx status (Celery retries up to _MAX_RETRIES times)
    """
    attempt = self.request.retries + 1
    _logger.info(
        "Delivering webhook for job %s to %s (attempt %d/%d)",
        job_id[:8], webhook_url[:80], attempt, _MAX_RETRIES + 1,
    )

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "BridgeLeads-Webhook/1.0",
        "X-BridgeLeads-Event": payload.get("event", "job.completed"),
        "X-BridgeLeads-Job-Id": job_id,
        "X-BridgeLeads-Delivery": f"{job_id}:{attempt}",
    }
    # Also surface the HMAC signature as a header (common webhook pattern,
    # Stripe-style), alongside the in-payload field.
    if payload.get("signature"):
        headers["X-BridgeLeads-Signature"] = payload["signature"]

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        _logger.warning(
            "Webhook %s delivery network error (attempt %d): %s",
            job_id[:8], attempt, str(exc)[:200],
        )
        raise  # Celery autoretry_for catches this

    # Non-2xx → treat as retryable failure
    if resp.status_code >= 400:
        _logger.warning(
            "Webhook %s delivery %d: %d %s",
            job_id[:8], attempt, resp.status_code, resp.text[:200],
        )
        if attempt >= _MAX_RETRIES + 1:
            # Final failure — log + return without raising so the job
            # doesn't appear as errored. Webhook failure is non-fatal
            # for the scrape job.
            _logger.error(
                "Webhook %s giving up after %d attempts (final %d)",
                job_id[:8], attempt, resp.status_code,
            )
            return {
                "status": "failed",
                "status_code": resp.status_code,
                "response_excerpt": resp.text[:500],
                "attempts": attempt,
            }
        # Retry via Celery's mechanism
        raise self.retry(
            exc=Exception(f"HTTP {resp.status_code}: {resp.text[:200]}"),
            countdown=_BACKOFF_BASE * (5 ** self.request.retries),
        )

    _logger.info(
        "Webhook %s delivered: %d (attempt %d)",
        job_id[:8], resp.status_code, attempt,
    )
    return {
        "status": "delivered",
        "status_code": resp.status_code,
        "response_excerpt": resp.text[:500],
        "attempts": attempt,
    }
