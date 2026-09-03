"""Thread 3 — native dialer outbox transport.

Drains `dialer_deliveries` rows for outbox-based connectors (e.g. PhoneBurner),
one POST per contact, with durable per-row state so a replay re-sends ONLY the
non-delivered rows. Separate from `deliver_job_webhook` on purpose: it must NOT
touch the shipped generic webhook / job-completion hot path (Codex).

SECURITY (docs/dialer_connectors_spike.md), all enforced here:
- Vendor credentials are RE-READ from the DB inside this task and built into the
  Authorization header just before the POST — never passed as a Celery task arg
  (which would serialize the token into the Redis broker / result backend). The
  only task arg is `job_id`.
- Tenant isolation under the RLS-bypassing system session: the config re-read uses
  the owner-match (ScraperConfig.user_id == Job.user_id) and every Result/outbox
  read filters by user_id.
- The built request URL's host MUST be in the connector's hardcoded ALLOWED_HOSTS,
  AND pass validate_outbound_webhook (HTTPS, no private/metadata IP), before any
  POST — PII + a bearer token can't reach an arbitrary host.
- Vendor responses (which often echo contact PII on error) are NEVER logged or
  stored: we keep only the status code + a narrowly-extracted contact id. Logs
  carry host + status + the outbox row id, never the token/PII/body.
"""
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests

from src.api.lead_actionability import actionable_condition
from src.api.middleware.security import validate_outbound_webhook
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.dialer_outbox")

# Per-task chunk: bound runtime + spread vendor rate-limit load. 500 leads drain
# across ~10 self-re-enqueued invocations.
_CHUNK = 50
_HTTP_TIMEOUT = 15

_SESSION = requests.Session()
_SESSION.trust_env = False  # don't let an ambient proxy bypass the SSRF resolve check


def _extract_contact_id(resp: requests.Response) -> str | None:
    """Best-effort: pull ONLY a vendor contact id from the response, never PII/body."""
    try:
        data = resp.json()
    except (ValueError, requests.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Look at a couple of common shapes without ever returning the full body.
    for key in ("id", "contact_id"):
        val = data.get(key)
        if isinstance(val, (str, int)):
            return str(val)[:128]
    inner = data.get("contact")
    if isinstance(inner, dict) and isinstance(inner.get("id"), (str, int)):
        return str(inner["id"])[:128]
    return None


@app.task(name="src.workers.dialer_outbox.process_dialer_outbox")
def process_dialer_outbox(job_id: str) -> None:
    """Drain up to _CHUNK pending dialer_deliveries rows for one job, one POST each.

    Re-enqueues itself while pending rows remain. A row that fails to deliver is
    marked 'failed' (not retried in-loop) and is replay-eligible; a delivered row
    is terminal so a replay never re-pushes it.
    """
    from sqlalchemy import select

    from src.api.tax_filters import tax_cap_condition
    from src.db.models import DialerDelivery, Job, Result, ScraperConfig
    from src.db.session import system_sync_session
    from src.workers.dialer_connectors import get_connector

    today = datetime.now(UTC).date()

    with system_sync_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        # Owner-match re-read (system session bypasses RLS): the config MUST belong
        # to the same user as the job, or we could push one tenant's leads with
        # another's credentials/config.
        config = db.execute(
            select(ScraperConfig).where(
                ScraperConfig.id == job.scraper_config_id,
                ScraperConfig.user_id == job.user_id,
            )
        ).scalar_one_or_none()
        if config is None:
            return

        deliver = config.deliver or {}
        connector = get_connector(deliver.get("dialer_type"))

        pending = db.execute(
            select(DialerDelivery)
            .where(
                DialerDelivery.job_id == job_id,
                DialerDelivery.user_id == job.user_id,
                DialerDelivery.status == "pending",
            )
            .order_by(DialerDelivery.created_at)
            .limit(_CHUNK)
        ).scalars().all()
        if not pending:
            return

        # Config-level failure (e.g. missing/expired credentials): fail this chunk's
        # rows with a clean message — they stay replay-eligible after the user fixes
        # the config. Never surface the underlying value.
        try:
            connector.validate_config(deliver)
        except ValueError as exc:
            for row in pending:
                row.status = "failed"
                row.attempts += 1
                row.last_error = f"config invalid: {str(exc)[:180]}"
            db.commit()
            _logger.warning(
                "dialer_outbox job %s: config invalid (%s) — %d rows failed",
                job_id[:8], connector.VENDOR_ID, len(pending),
            )
            return

        for row in pending:
            row.attempts += 1
            # tax_cap_condition is self-scoping: non-tax rows (NULL
            # delinquent_bill_year) pass untouched, so this only ever HIDES a
            # tax-delinquent lead whose oldest unpaid year is past the 18-month
            # cap — it never changes which non-tax lead is selected.
            result = db.execute(
                select(Result).where(
                    Result.id == row.result_id,
                    Result.user_id == job.user_id,
                    tax_cap_condition(today),
                    # Standing rule (lead_actionability): no property AND no
                    # mailing address = not a lead — treated like the tax cap
                    # below (terminal skip, never dialed).
                    actionable_condition(),
                )
            ).scalar_one_or_none()
            if result is None:
                # Two reasons the row can be missing now: the result was deleted,
                # OR it's an out-of-window tax lead the cap intentionally hides.
                # Either way it's terminal (not a transient error): drop it as
                # 'skipped' so a replay never re-evaluates it. Re-check existence
                # without the cap only to log the accurate reason.
                exists = db.execute(
                    select(Result.id).where(
                        Result.id == row.result_id, Result.user_id == job.user_id
                    )
                ).first()
                if exists is not None:
                    row.status = "skipped"
                    row.last_error = "tax lead past 18-month cap"
                    _logger.info(
                        "dialer_outbox row %s: tax lead past 18-month cap — skipped",
                        str(row.id)[:8],
                    )
                else:
                    row.status = "failed"
                    row.last_error = "result row missing"
                db.commit()
                continue

            lead = {
                "id": str(result.id),
                "party_name": result.party_name,
                "phone": result.phone,
                "phone_type": result.phone_type,
                "phone_dnc_flag": result.phone_dnc_flag,
                "email": result.email,
                "property_address": result.property_address,
                "mailing_address": result.mailing_address,
            }
            job_meta = {
                "job_id": str(job.id),
                "scraper_config_id": str(config.id),
                "scraper_name": config.name,
                "county": config.county,
                "state": config.state,
                "record_type": config.record_type,
                # Credentials assembled HERE (DB re-read), passed to build_requests
                # only for the in-process POST — never enter a Celery message.
                "phoneburner_access_token": deliver.get("phoneburner_access_token"),
                "phoneburner_owner_id": deliver.get("phoneburner_owner_id"),
            }
            try:
                req = connector.build_requests([lead], job_meta)[0]
            except Exception as exc:  # noqa: BLE001 — one bad row must not stall the chunk
                row.status = "failed"
                row.last_error = f"build error: {str(exc)[:180]}"
                db.commit()
                continue

            _deliver_one(connector, req, row)
            # COMMIT THIS ROW'S OUTCOME BEFORE THE NEXT EXTERNAL POST (Codex P2):
            # if the worker is killed / the task is redelivered after a successful
            # POST, the row is already durably 'delivered' so the next drain/replay
            # won't re-POST it and duplicate a PhoneBurner contact. Per-row commit
            # is the at-most-once-per-contact guarantee (the chunk is small).
            db.commit()

        # More pending? Continue draining in a fresh task (bounded chunks).
        remaining = db.execute(
            select(DialerDelivery.id)
            .where(
                DialerDelivery.job_id == job_id,
                DialerDelivery.user_id == job.user_id,
                DialerDelivery.status == "pending",
            )
            .limit(1)
        ).first()

    if remaining is not None:
        process_dialer_outbox.delay(job_id)


def _deliver_one(connector, req: dict, row) -> None:
    """POST a single vendor contact request and stamp the outbox row in place.

    Mutates ``row`` (status/last_error/vendor_response_code/vendor_contact_id/
    delivered_at). Never raises — a per-row failure must not abort the chunk.
    """
    url = req["url"]
    host = urlparse(url).hostname or "?"

    # Host pin: the destination must be the connector's hardcoded vendor host.
    if host not in connector.ALLOWED_HOSTS:
        row.status = "failed"
        row.last_error = f"host {host} not in connector allowlist"
        _logger.error(
            "dialer_outbox row %s: blocked host %s (vendor=%s)",
            str(row.id)[:8], host, connector.VENDOR_ID,
        )
        return

    # SSRF defense-in-depth: HTTPS + resolves to a public IP, redirects refused.
    try:
        validate_outbound_webhook(url)
    except ValueError as exc:
        row.status = "failed"
        row.last_error = "blocked by SSRF guard"
        _logger.warning(
            "dialer_outbox row %s: SSRF block host %s: %s",
            str(row.id)[:8], host, str(exc)[:120],
        )
        return

    try:
        resp = _SESSION.post(
            url,
            json=req["body"],
            headers=req["headers"],
            timeout=_HTTP_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # Network error → replay-eligible. Log the EXCEPTION TYPE only (str(exc)
        # can contain the URL incl. no secret here, but keep it terse + host-only).
        row.status = "failed"
        row.last_error = f"network error: {type(exc).__name__}"
        _logger.warning(
            "dialer_outbox row %s: network error to %s (%s)",
            str(row.id)[:8], host, type(exc).__name__,
        )
        return

    row.vendor_response_code = resp.status_code
    if 200 <= resp.status_code < 300:
        row.status = "delivered"
        row.delivered_at = datetime.now(UTC)
        row.last_error = None
        row.vendor_contact_id = _extract_contact_id(resp)
        _logger.info(
            "dialer_outbox row %s delivered to %s (%d)",
            str(row.id)[:8], host, resp.status_code,
        )
    else:
        # Redirect or 4xx/5xx → failed, replay-eligible. NEVER store/log the body
        # (vendor error bodies echo submitted contact PII).
        row.status = "failed"
        row.last_error = f"HTTP {resp.status_code}"
        _logger.warning(
            "dialer_outbox row %s: %s returned %d",
            str(row.id)[:8], host, resp.status_code,
        )
