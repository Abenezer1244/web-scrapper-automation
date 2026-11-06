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
from urllib.parse import parse_qsl, unquote, urlparse

import requests
import urllib3

from src.api.middleware.security import validate_outbound_webhook
from src.privacy.authorize import RuleSnapshotError, authorize, load_rule_snapshot
from src.utils.logger import setup_logger
from src.utils.pinned_http import is_blocked_destination, log_host, pinned_session
from src.workers import app

_logger = setup_logger("worker.webhook_delivery")

# Retry policy: 3 attempts total with exponential backoff
_MAX_RETRIES = 3
_BACKOFF_BASE = 5  # seconds — actual waits: 5s, 25s, 125s

# Webhook target timeout — enough for slow Zapier/Make cold starts
_HTTP_TIMEOUT = 15

# The response body is only ever used for a log/result excerpt, so read no more
# than that. The destination is customer-chosen: reading its whole body let a
# gzip bomb or a 32 MB error page into the memory of a worker shared with every
# tenant's scrapes (audit 2026-09-25, E-1). The body is read RAW and never
# decoded, so no stack of Content-Encodings can expand it.
_RESPONSE_EXCERPT_BYTES = 2048

# A per-read timeout does not bound a server that drips a byte every 14s. The
# task gets a wall-clock ceiling instead, far above any honest endpoint. Hitting
# it fails the attempt without a retry, which is the intent for such a server.
_TASK_SOFT_TIME_LIMIT = 60
_TASK_TIME_LIMIT = 90


def _url_secrets(webhook_url: str) -> list[str]:
    """Every fragment of the URL that may be a credential, raw and decoded.

    Catch-hook services put the secret in the PATH (/hooks/catch/123/abcdef),
    others in the query or userinfo. Short fragments are skipped so that words
    like "hooks" or "api" do not blank out the whole excerpt.
    """
    try:
        parts = urlparse(webhook_url)
    except ValueError:
        # Unparseable (see pinned_http.log_host): treat the whole URL as secret.
        return [webhook_url] if webhook_url else []
    # Userinfo: some services put the token in the USERNAME (https://TOKEN@host).
    found = [parts.password or "", parts.username or ""]
    found += [seg for seg in parts.path.split("/") if len(seg) >= 6]
    found += [value for _, value in parse_qsl(parts.query, keep_blank_values=False) if len(value) >= 4]
    # parse_qsl decodes, so keep the RAW values too: an echo of `ab%2Bcd` must match.
    found += [p.partition("=")[2] for p in parts.query.split("&") if len(p.partition("=")[2]) >= 4]
    found += [unquote(f) for f in found]
    return sorted({f for f in found if f}, key=len, reverse=True)


def _redact_url_secrets(text: str, webhook_url: str) -> str:
    secrets = _url_secrets(webhook_url)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    # The excerpt may have been cut mid-secret: drop a trailing partial. A cut
    # after fewer than 4 characters is left alone; that little is not a secret.
    for secret in secrets:
        for n in range(len(secret) - 1, 3, -1):
            if text.endswith(secret[:n]):
                return text[: -n] + "<redacted>"
    return text


def _response_excerpt(text: str) -> str:
    """What a delivery logs and keeps (Celery result) of the endpoint's reply.

    Its length, never its content, for every event (P0-04). Dialer pushes carry
    lead PII and any endpoint may echo the request back; and a body that never
    surfaces keeps an SSRF through a rebinding host blind. The status code
    carries the outcome.
    """
    return f"<response body: {len(text or '')} chars, not kept>"


def _read_excerpt(resp: requests.Response, webhook_url: str) -> str:
    """Up to _RESPONSE_EXCERPT_BYTES of the RAW body, then close the socket.

    Undecoded on purpose: the request asks for identity encoding, and a server
    that compresses anyway gets a placeholder rather than a decompressor.
    """
    encoding = (resp.headers.get("Content-Encoding") or "identity").strip().lower()
    raw = b""
    try:
        raw = resp.raw.read(_RESPONSE_EXCERPT_BYTES, decode_content=False) or b""
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
        # Deliberately NOT `Exception`: Celery's SoftTimeLimitExceeded subclasses
        # it, and the wall-clock limit must end the task, not shorten an excerpt.
        # The status line already arrived and decides the outcome; the body is
        # only an excerpt, so a broken read shortens it rather than failing.
        _logger.debug("webhook response excerpt cut short: %s", type(exc).__name__)
    finally:
        resp.close()
    if encoding != "identity":
        return f"<{encoding[:40]} body, not decoded>"
    text = raw[:_RESPONSE_EXCERPT_BYTES].decode("utf-8", errors="replace")
    return _redact_url_secrets(text, webhook_url)

# The socket connects only to an address the SSRF policy approved, resolved in
# the same step (DNS-rebinding fix, audit 2026-09-25 F-03). Proxies are refused,
# including an ambient HTTPS_PROXY, since a proxy would resolve for us.
_SESSION = pinned_session()


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


def _blocked(job_id: str, host: str, attempt: int) -> dict:
    """Result for a destination the SSRF policy refuses: alert ops, never retry."""
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


@app.task(
    name="src.workers.webhook_delivery.deliver_job_webhook",
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_BACKOFF_BASE,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    soft_time_limit=_TASK_SOFT_TIME_LIMIT,
    time_limit=_TASK_TIME_LIMIT,
)
def deliver_job_webhook(
    self,
    job_id: str,
    webhook_url: str | None = None,
    payload: dict | None = None,
    sealed: str | None = None,
) -> dict:
    """POST a job-completion payload to the configured webhook URL.

    Args:
        job_id: The job UUID (for logging correlation only)
        webhook_url: Full URL to POST to
        payload: Pre-built JSON payload (from build_webhook_payload)
        sealed: P0-04b: {"url", "payload"} via seal_task_args, so the URL (which
            can carry a secret), signed download links and dialer leads never sit
            in the broker as plaintext. webhook_url/payload remain for messages
            from the previous release; a sealed envelope that does not open
            raises, never falls back to them.

    Returns:
        Dict with {status_code, response_excerpt, attempts}.

    Raises:
        requests.RequestException: on network error (Celery auto-retries)
        Exception: on non-2xx status (Celery retries up to _MAX_RETRIES times)
    """
    if sealed is not None:
        from src.utils.crypto import unseal_task_args

        args = unseal_task_args(sealed, required=("url", "payload"))
        webhook_url, payload = args["url"], args["payload"]
    return _post(self, job_id, webhook_url, payload)


def _post(task, job_id: str, webhook_url: str, payload: dict) -> dict:
    """One delivery attempt for a bound webhook task: SSRF re-check, POST, and the
    retry/give-up policy (task.retry on a retryable failure)."""
    attempt = task.request.retries + 1
    host = log_host(webhook_url)
    _excerpt = _response_excerpt

    # SSRF guard: re-validate the destination immediately before the POST so a
    # host that rebinds to a private/metadata IP after config-save is caught
    # with a clear reason. A blocked target is a permanent config problem, so it
    # returns WITHOUT raising and Celery does not retry it. Never log the full
    # URL (it may carry query-string secrets); host only.
    #
    # This check alone cannot stop DNS rebinding: the POST resolves the host
    # again. _SESSION closes that gap (F-03): its connections resolve, check and
    # connect in one step, so a host that answers public here and private at
    # connect time is refused there, and lands in _blocked() below too.
    try:
        validate_outbound_webhook(webhook_url)
    except ValueError as exc:
        _logger.warning(
            "Webhook %s blocked by SSRF guard (host=%s): %s",
            job_id[:8], host, _redact_url_secrets(str(exc), webhook_url),
        )
        return _blocked(job_id, host, attempt)

    _logger.info(
        "Delivering webhook for job %s to host %s (attempt %d/%d)",
        job_id[:8], host, attempt, _MAX_RETRIES + 1,
    )

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "BridgeLeads-Webhook/1.0",
        "Accept-Encoding": "identity",  # the excerpt is never decompressed (E-1)
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
            stream=True,  # the body is read under _RESPONSE_EXCERPT_BYTES below
        )
    except requests.RequestException as exc:
        if is_blocked_destination(exc):
            _logger.warning(
                "Webhook %s blocked at connect by SSRF guard (host=%s)", job_id[:8], host,
            )
            return _blocked(job_id, host, attempt)
        # str(exc) from requests embeds the full URL, query-string secret and
        # all, and so would the re-raised exception in Celery's own task log and
        # result backend (audit 2026-09-25, E-2). Type and host only, and the
        # original is not chained, so its message goes nowhere.
        reason = type(exc).__name__
        _logger.warning(
            "Webhook %s delivery network error (attempt %d): %s to host %s",
            job_id[:8], attempt, reason, host,
        )
        raise requests.ConnectionError(
            f"webhook delivery network error: {reason} (host {host})"
        ) from None  # Celery autoretry_for catches this
    except ValueError as exc:
        # After RequestException on purpose: some of those (InvalidURL…) are also
        # ValueErrors and keep the retry path above. A PLAIN ValueError here is
        # requests parsing a 3xx Location to set resp.next even with
        # allow_redirects=False: its text is the receiver's authority, which can
        # carry a credential. Permanent, like the 3xx case below: type and host
        # only, never retried, never raised into Celery's failure log.
        _logger.warning(
            "Webhook %s endpoint sent an unparseable response (%s) from host %s",
            job_id[:8], type(exc).__name__, host,
        )
        return {"status": "failed", "reason": "invalid_response", "attempts": attempt}
    body_text = _read_excerpt(resp, webhook_url)

    # Redirects are disabled, so a 3xx is a misconfigured endpoint, not a
    # success — and following the Location is exactly the SSRF vector we
    # are closing. Treat as a permanent (non-retryable) failure.
    if 300 <= resp.status_code < 400:
        # Log the redirect target HOST only — a Location header can itself carry
        # query-string secrets (same log-leak class as the webhook URL, Codex).
        _loc_host = log_host(resp.headers.get("Location"))
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
            job_id[:8], attempt, resp.status_code, _excerpt(body_text),
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
                "response_excerpt": _excerpt(body_text),
                "attempts": attempt,
            }
        # Retry via Celery's mechanism. The exc message reaches logs + the result
        # backend, so it carries the status only, for every event: the body is the
        # customer endpoint's own text, already logged (bounded, redacted) above.
        raise task.retry(
            exc=Exception(f"HTTP {resp.status_code}"),
            countdown=_BACKOFF_BASE * (5 ** task.request.retries),
        )

    _logger.info(
        "Webhook %s delivered: %d (attempt %d)",
        job_id[:8], resp.status_code, attempt,
    )
    return {
        "status": "delivered",
        "status_code": resp.status_code,
        "response_excerpt": _excerpt(body_text),
        "attempts": attempt,
    }


# Phase 6 P4b-2: the generic dialer push carries row ids, not people. Each attempt
# re-reads the destination and the rows and authorizes every row against the privacy
# suppression rules immediately before the POST, so a rule that takes effect while the
# message waits (or between retries) is honoured. Delivery stays AT-LEAST-ONCE, as it
# was: a retry after a timeout can repeat a POST the receiver already took, and the
# payload's batch.id + per-lead external_id are the documented dedup keys.


@app.task(
    name="src.workers.webhook_delivery.deliver_dialer_push",
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_BACKOFF_BASE,
    autoretry_for=(requests.RequestException, RuleSnapshotError),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    soft_time_limit=_TASK_SOFT_TIME_LIMIT,
    time_limit=_TASK_TIME_LIMIT,
)
def deliver_dialer_push(self, job_id: str, result_ids: list[str], total: int) -> dict:
    """Rebuild and POST one generic dialer push from the database (see above).

    ``result_ids`` are the rows the sweep selected (at most DIALER_PUSH_CAP),
    ``total`` the dialer-ready count it saw. Nothing personal is in the message: the
    URL and signing secret are re-read from the owner-matched config, and the plan is
    re-checked, at every attempt. A row that is gone, no longer ready or not
    authorized is left out; when none is left, nothing is sent. Rules that cannot be
    loaded raise RuleSnapshotError, which retries: never a send without the check.
    """
    from sqlalchemy import func, select

    from src.api.dialer_filters import dialer_ready_conditions
    from src.api.lead_actionability import actionable_condition
    from src.api.tax_filters import tax_cap_condition
    from src.config.constants import BUSINESS_FEATURES_PLANS
    from src.db.models import Job, Result, ScraperConfig, User
    from src.db.session import system_sync_session
    from src.workers.dialer_connectors import get_connector

    with system_sync_session() as db:
        row = db.execute(
            select(Job, ScraperConfig)
            .join(ScraperConfig, (ScraperConfig.id == Job.scraper_config_id)
                  & (ScraperConfig.user_id == Job.user_id))
            .join(User, User.id == Job.user_id)
            .where(Job.id == job_id,
                   func.lower(func.trim(User.plan)).in_(BUSINESS_FEATURES_PLANS))
        ).first()
        if row is None:
            _logger.info("Dialer push %s dropped: job, config or plan no longer eligible",
                         job_id[:8])
            return {"status": "skipped", "reason": "not_eligible"}
        job, config = row
        deliver = config.deliver or {}
        connector = get_connector(deliver.get("dialer_type"))
        url = deliver.get("dialer_webhook_url")
        if connector.uses_outbox or not url:
            return {"status": "skipped", "reason": "no_generic_webhook"}

        snapshot = load_rule_snapshot(db)  # RuleSnapshotError -> retry (autoretry_for)
        rows = db.execute(
            select(Result)
            .where(Result.id.in_(result_ids), Result.job_id == job.id,
                   Result.user_id == job.user_id,
                   *dialer_ready_conditions(include_unknown_dnc=True),
                   Result.is_duplicate.is_(False),
                   tax_cap_condition(datetime.now(UTC).date()),
                   actionable_condition())
            .order_by(Result.id)
        ).scalars().all()
        leads = [
            {"id": r.id, "party_name": r.party_name, "phone": r.phone,
             "phone_type": r.phone_type, "phone_dnc_flag": r.phone_dnc_flag,
             "email": r.email, "property_address": r.property_address,
             "mailing_address": r.mailing_address}
            for r in rows
            if authorize(snapshot, r, record_type=config.record_type, county=config.county,
                         state=config.state).deliverable
        ]
        job_meta = {
            "job_id": str(job.id), "scraper_config_id": str(config.id),
            "scraper_name": config.name, "county": config.county, "state": config.state,
            "record_type": config.record_type, "total_dialer_ready_count": total,
            "dialer_webhook_url": url,
            "dialer_webhook_secret": deliver.get("dialer_webhook_secret"),
        }
    withheld = len(result_ids) - len(leads)
    if withheld:
        _logger.info("Dialer push %s: %d of %d selected rows left out at send time",
                     job_id[:8], withheld, len(result_ids))
    if not leads:
        return {"status": "skipped", "reason": "no_deliverable_rows"}
    [req] = connector.build_requests(leads, job_meta)
    return _post(self, job_id, req["url"], req["payload"])
