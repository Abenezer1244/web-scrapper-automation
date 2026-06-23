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
from urllib.parse import urlparse

import requests

from src.api.middleware.security import validate_outbound_webhook
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.webhook_delivery")

# Retry policy: 3 attempts total with exponential backoff
_MAX_RETRIES = 3
_BACKOFF_BASE = 5  # seconds — actual waits: 5s, 25s, 125s

# Webhook target timeout — enough for slow Zapier/Make cold starts
_HTTP_TIMEOUT = 15

# Dedicated session with trust_env=False: an ambient HTTPS_PROXY/NO_PROXY in
# the worker env could otherwise move DNS resolution off-box (to the proxy),
# bypassing the SSRF guard's resolved-IP check. We resolve + connect locally.
_SESSION = requests.Session()
_SESSION.trust_env = False


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
        },
    }
    payload["signature"] = _sign_payload(payload, webhook_secret)
    return payload


# Phase 5: generic dialer push. Cap leads per POST conservatively — dialer-ready
# rows are wide (name/phone/email/addresses) and Zapier/dialer webhook bodies
# have size limits. `truncated` + `total_dialer_ready_count` tell the consumer
# how much was held back; the full set stays available via the normal export.
DIALER_PUSH_CAP = 500
_DIALER_SCHEMA_VERSION = "2026-06-05.dialer_ready.v1"


def build_dialer_push_payload(
    job_id: str,
    scraper_config_id: str,
    scraper_name: str,
    county: str,
    state: str,
    record_type: str,
    leads: list[dict],
    total_dialer_ready_count: int,
    webhook_secret: str | None,
) -> dict:
    """Build the dialer-push payload — dialer-ready LEAD ROWS for a dialer/Zapier.

    `leads` is the ALREADY-capped, dialer-ready list (each a flat dict with id +
    contact fields). Each lead gets a stable `external_id` (and the scraper's
    county/state/record_type flattened in) so a consumer can create/dedupe
    contacts; `batch.id` is the stable per-job batch key for retry-safe dedup
    (the per-attempt id is the X-BridgeLeads-Delivery header, NOT this). PII
    (phone/email) travels in the body — the caller guarantees HTTPS + SSRF guard
    + a per-user destination, and the transport logs host only.
    """
    now = datetime.now(UTC)
    enriched: list[dict] = []
    for ld in leads:
        rid = str(ld.get("id"))
        # Honest DNC labeling (Codex): BridgeLeads has no DNC feed, so a row is
        # "clear" only when phone_dnc_flag is explicitly False; NULL = "unknown".
        # We exclude KNOWN-DNC upstream but never claim an unknown number is
        # confirmed callable — the consumer/dialer must scrub (see dnc_scrubbed).
        dnc = ld.get("phone_dnc_flag")
        dnc_status = "clear" if dnc is False else ("dnc" if dnc is True else "unknown")
        enriched.append({
            "external_id": f"bridgeleads:result:{rid}",
            "id": rid,
            "party_name": ld.get("party_name"),
            "phone": ld.get("phone"),
            "phone_type": ld.get("phone_type"),
            "dnc_status": dnc_status,
            "email": ld.get("email"),
            "property_address": ld.get("property_address"),
            "mailing_address": ld.get("mailing_address"),
            "county": county,
            "state": state,
            "record_type": record_type,
        })

    payload = {
        "event": "leads.dialer_ready",
        "schema_version": _DIALER_SCHEMA_VERSION,
        "delivered_at": now.isoformat(),
        # BridgeLeads does NOT scrub the DNC registry — leads carry valid phones
        # that are not KNOWN-DNC, but unknown-DNC numbers are included. The
        # receiving dialer is the authoritative DNC/TCPA compliance layer.
        "dnc_scrubbed": False,
        "batch": {"id": f"job:{job_id}:dialer-ready:v1", "job_id": job_id},
        "scraper": {
            "id": scraper_config_id,
            "name": scraper_name,
            "county": county,
            "state": state,
            "record_type": record_type,
        },
        "lead_count": len(enriched),
        "total_dialer_ready_count": total_dialer_ready_count,
        "truncated": total_dialer_ready_count > len(enriched),
        "leads": enriched,
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
    host = urlparse(webhook_url).hostname or "?"
    # Phase 5: dialer pushes carry lead PII in the request body. A rejecting
    # endpoint may echo the submitted fields back in its response, so we must NOT
    # log or persist (Celery result backend) the response body for these events
    # (Codex). Redact it to a status-only marker.
    _redact_response = payload.get("event") == "leads.dialer_ready"

    def _excerpt(text: str) -> str:
        return "<redacted: dialer push>" if _redact_response else text[:500]

    # SSRF guard (authoritative): re-validate the destination immediately
    # before the POST so a host that rebinds to a private/metadata IP after
    # config-save is still caught. A blocked target is a permanent config
    # problem — return WITHOUT raising so Celery does not retry it. Never
    # log the full URL (it may carry query-string secrets); host only.
    try:
        validate_outbound_webhook(webhook_url)
    except ValueError as exc:
        _logger.warning(
            "Webhook %s blocked by SSRF guard (host=%s): %s",
            job_id[:8], host, exc,
        )
        from src.workers.ops_alerts import send_ops_alert
        send_ops_alert(
            "webhook_blocked", job_id,
            "Webhook target blocked (SSRF guard)",
            f"Completion webhook for job {job_id} was blocked: host {host} is "
            f"not a permitted target. The user's configured webhook will never "
            f"fire until they fix the URL.",
        )
        return {
            "status": "blocked",
            "reason": "webhook target not permitted",
            "attempts": attempt,
        }

    _logger.info(
        "Delivering webhook for job %s to host %s (attempt %d/%d)",
        job_id[:8], host, attempt, _MAX_RETRIES + 1,
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
        resp = _SESSION.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,  # don't follow 30x to a (possibly internal) Location
        )
    except requests.RequestException as exc:
        _logger.warning(
            "Webhook %s delivery network error (attempt %d): %s",
            job_id[:8], attempt, str(exc)[:200],
        )
        raise  # Celery autoretry_for catches this

    # Redirects are disabled, so a 3xx is a misconfigured endpoint, not a
    # success — and following the Location is exactly the SSRF vector we
    # are closing. Treat as a permanent (non-retryable) failure.
    if 300 <= resp.status_code < 400:
        # Log the redirect target HOST only — a Location header can itself carry
        # query-string secrets (same log-leak class as the webhook URL, Codex).
        _loc_host = urlparse(resp.headers.get("Location") or "").hostname or "?"
        _logger.warning(
            "Webhook %s endpoint returned redirect %d to host %s — not following",
            job_id[:8], resp.status_code, _loc_host,
        )
        return {
            "status": "failed",
            "reason": "redirect_not_followed",
            "status_code": resp.status_code,
            "attempts": attempt,
        }

    # Non-2xx → treat as retryable failure
    if resp.status_code >= 400:
        _logger.warning(
            "Webhook %s delivery %d: %d %s",
            job_id[:8], attempt, resp.status_code, _excerpt(resp.text),
        )
        if attempt >= _MAX_RETRIES + 1:
            # Final failure — log + return without raising so the job
            # doesn't appear as errored. Webhook failure is non-fatal
            # for the scrape job.
            _logger.error(
                "Webhook %s giving up after %d attempts (final %d)",
                job_id[:8], attempt, resp.status_code,
            )
            from src.workers.ops_alerts import send_ops_alert
            send_ops_alert(
                "webhook_delivery", job_id,
                "Webhook delivery failed",
                f"Completion webhook for job {job_id} gave up after {attempt} "
                f"attempts (last status {resp.status_code}, host {host}).",
            )
            return {
                "status": "failed",
                "status_code": resp.status_code,
                "response_excerpt": _excerpt(resp.text),
                "attempts": attempt,
            }
        # Retry via Celery's mechanism. The exc message reaches logs + the result
        # backend, so keep the body out of it for dialer pushes.
        retry_detail = (
            f"HTTP {resp.status_code}" if _redact_response
            else f"HTTP {resp.status_code}: {resp.text[:200]}"
        )
        raise self.retry(
            exc=Exception(retry_detail),
            countdown=_BACKOFF_BASE * (5 ** self.request.retries),
        )

    _logger.info(
        "Webhook %s delivered: %d (attempt %d)",
        job_id[:8], resp.status_code, attempt,
    )
    return {
        "status": "delivered",
        "status_code": resp.status_code,
        "response_excerpt": _excerpt(resp.text),
        "attempts": attempt,
    }
