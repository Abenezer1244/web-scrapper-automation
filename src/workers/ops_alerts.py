"""M6: operational alerting for watchdog/canary/batch failures (audit item).

Before this module those failures were LOG-ONLY — nobody gets paged when a
scraper portal goes down or a job dies permanently. Alerts go out on two
independent routes:

- e-mail to ``settings.OPS_ALERT_EMAIL`` via Resend (already integrated for delivery);
- a JSON POST to ``settings.OPS_ALERT_WEBHOOK_URL`` (a Better Stack incoming webhook),
  so an alert still reaches someone when Resend, or the inbox, is the thing that broke
  (P0-09: "alerting is one unproven channel").

Design rules:
- **Best-effort, never raises**: an alerting failure must never fail the
  watchdog/canary/sweep that called it. Every path is caught and logged.
- **Routes are independent**: each has its own try/except and its own cooldown, so
  one route failing never stops or mutes the other.
- **Cooldown** per (route, kind, key) in Redis (SET NX EX) so a connector that is
  down all day alerts once per ``OPS_ALERT_COOLDOWN_SECONDS``, not once per
  canary tick. Redis unavailable -> SEND anyway (noisy beats silent for ops).
- **Disabled by default**: both settings empty = no-op (dev/CI safe).
- No PII in alert bodies: job/connector identifiers only, never lead data.
- The webhook URL is the credential: it is never logged, and webhook errors are
  logged by exception class and status code only (their messages embed the URL).
"""
import asyncio
import html
import json
import urllib.request

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.ops_alerts")

_COOLDOWN_PREFIX = "ops_alert:"

# Connect and read timeout, in seconds, for the webhook POST. Always off the
# event loop (see send_ops_alert), so the worst case costs a worker thread, not a request.
_WEBHOOK_TIMEOUT_SECONDS = 5.0


def _cooldown_acquired(kind: str, key: str, route: str = "email") -> bool:
    """True when this (kind, key) is NOT in cooldown for ``route`` (and marks it). Fails open.

    Per route, so a Better Stack failure inside an e-mail cooldown is not suppressed for
    6h, and a newly configured webhook is not muted by buckets set before it existed
    (Codex P1). E-mail keeps the original key so in-flight cooldowns survive the deploy.
    """
    try:
        import redis as sync_redis

        r = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        try:
            return bool(
                r.set(
                    _cooldown_key(kind, key, route),
                    "1",
                    nx=True,
                    ex=settings.OPS_ALERT_COOLDOWN_SECONDS,
                )
            )
        finally:
            r.close()
    except Exception as exc:  # noqa: BLE001 — alerting must not depend on Redis
        _logger.warning("ops alert cooldown check failed (sending anyway): %s", str(exc)[:120])
        return True


def _cooldown_key(kind: str, key: str, route: str) -> str:
    bucket = f"{kind}:{key}" if route == "email" else f"{route}:{kind}:{key}"
    return f"{_COOLDOWN_PREFIX}{bucket}"


def _cooldown_release(kind: str, key: str, route: str) -> None:
    """Free a route's bucket after a FAILED send, so the next occurrence retries instead
    of staying muted for the whole cooldown (Codex P2). Never raises."""
    try:
        import redis as sync_redis

        r = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        try:
            r.delete(_cooldown_key(kind, key, route))
        finally:
            r.close()
    except Exception as exc:  # noqa: BLE001 — alerting must not depend on Redis
        _logger.warning("ops alert cooldown release failed: %s", str(exc)[:120])


def _session_for_persist():
    """The session the durable write uses.

    A named seam so the behaviour above can be tested against a fake, rather than only
    asserted against source text (Codex: most of the first round of tests inspected
    source strings and would not have caught a wrong value or a blocking write).
    """
    from src.db.session import system_sync_session

    return system_sync_session()


def _persist_ops_alert(kind: str, key: str, subject: str, outcome: str) -> None:
    """Best-effort durable record of an ops alert. NEVER raises.

    Written to audit_events (event='ops_alert', user_id NULL) rather than to a new
    table: system-written rows with a NULL user_id are already an established pattern
    there, so this needs no migration. Deliberately records EVERY alert-worthy
    occurrence, not just the ones that clear the cooldown — "this fired daily
    for four weeks" is the shape of the question these rows exist to answer.

    ``outcome`` is e.g. ``[sent] [email:ok webhook:fail]``. The first bracket is
    exactly ``[sent]`` or ``[undelivered]`` (the ops dashboard counts the latter); the
    second names each route's result, because "e-mailed but Better Stack rejected it"
    is a fact only the row can tell later.

    created_at is set client-side so the INSERT emits no RETURNING: under FORCE RLS the
    app role may INSERT into audit_events but not SELECT, and a server_default would
    trigger a RETURNING that RLS then denies (same trap as _persist_audit_event in
    src/api/middleware/security.py).
    """
    try:
        import uuid as _uuid
        from datetime import UTC, datetime

        from src.db.models import AuditEvent

        with _session_for_persist() as db:
            db.add(
                AuditEvent(
                    id=str(_uuid.uuid4()),
                    event="ops_alert",
                    user_id=None,
                    path=f"{kind}:{key}"[:256],
                    detail=f"{outcome} {subject}"[:512],
                    created_at=datetime.now(UTC),
                )
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 — durability is additive, never fatal
        _logger.warning("ops_alert audit insert failed (alert still logged): %s", str(exc)[:200])


def _send_email(kind: str, key: str, subject: str, body: str) -> None:
    """One e-mail via Resend. Raises on failure; the caller records the outcome."""
    import resend

    resend.api_key = settings.RESEND_API_KEY
    from src.utils.email_layout import build_payload, header_text

    # Internal ops mail, so it deliberately keeps the plain monospace dump
    # rather than the customer-facing layout. It still goes through
    # build_payload so the From display name and Reply-To are the same
    # single source as every other send site, and it carries a
    # text/plain part (it had none, which hurts spam scoring).
    meta = f"kind={kind} key={key} · cooldown {settings.OPS_ALERT_COOLDOWN_SECONDS}s"
    resend.Emails.send(build_payload(
        to=[settings.OPS_ALERT_EMAIL],
        subject=f"[BridgeLeads OPS] {header_text(subject)}",
        html_body=(
            "<div style='font-family:monospace;font-size:13px;color:#111827'>"
            f"<p style='color:#111827'><b>{html.escape(subject)}</b></p>"
            f"<pre style='white-space:pre-wrap;color:#111827'>{html.escape(body)}</pre>"
            f"<p style='color:#6B7280'>{html.escape(meta)}</p>"
            "</div>"
        ),
        text_body=f"{subject}\n\n{body}\n\n{meta}",
    ))


def _send_webhook(kind: str, key: str, subject: str, body: str) -> None:
    """One POST to the Better Stack incoming webhook. Raises on failure.

    Better Stack is configured on its side to start an incident when ``status`` contains
    "alert", taking the title from ``title``, the cause from ``description`` and the
    alert id from ``alert_id``. ``alert_id`` is ``kind:key``: one logical incident per
    cooldown bucket. Incidents are point-in-time and resolved by hand in Better Stack;
    nothing here sends a resolve.

    Stdlib ``urllib.request`` on purpose: it logs nothing. httpx logs
    ``HTTP Request: POST <url>`` at INFO and urllib3 (under requests) logs the path at
    DEBUG, and the Better Stack token IS the path (Codex P1). Redirects are refused, so
    only a 2xx counts as delivered, never a redirect to some login page (Codex P2).
    """
    url = settings.OPS_ALERT_WEBHOOK_URL
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError("OPS_ALERT_WEBHOOK_URL is not an http(s) URL")
    req = urllib.request.Request(  # noqa: S310 — scheme checked on the line above
        url,
        data=json.dumps({
            "status": "alert",
            "alert_id": f"{kind}:{key}",
            "title": f"[BridgeLeads OPS] {subject}",
            "description": body,
            "kind": kind,
            "key": key,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # A 3xx with no redirect handler raises HTTPError, like a 4xx/5xx does.
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=_WEBHOOK_TIMEOUT_SECONDS) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"unexpected status {resp.status}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):
        return None


def _route(name: str, send, kind: str, key: str, subject: str, body: str) -> str:
    """Run one route behind its own cooldown. Returns ok / cooldown / fail. Never raises."""
    if not _cooldown_acquired(kind, key, name):
        _logger.info("ops alert suppressed (%s cooldown) [%s:%s] %s", name, kind, key, subject)
        return "cooldown"
    try:
        send(kind, key, subject, body)
    except Exception as exc:  # noqa: BLE001 — never fail the caller
        if name == "webhook":
            # Class + status only, never str(exc) or exc_info: the URL is the credential
            # and must not reach a log line by any route (Codex P2).
            status = getattr(exc, "code", None)
            _logger.error(
                "ops alert webhook failed [%s:%s]: %s status=%s",
                kind, key, type(exc).__name__, status,
            )
        else:
            _logger.error("ops alert %s failed [%s:%s]: %s", name, kind, key, str(exc)[:200])
        _cooldown_release(kind, key, name)
        return "fail"
    _logger.info("ops alert sent via %s [%s:%s] %s", name, kind, key, subject)
    return "ok"


def _send_and_record(kind: str, key: str, subject: str, body: str) -> bool:
    """Every configured route, then one durable row. True when any route delivered."""
    results = {"email": "off", "webhook": "off"}
    try:
        if settings.OPS_ALERT_EMAIL and settings.RESEND_API_KEY:
            results["email"] = _route("email", _send_email, kind, key, subject, body)
        elif settings.OPS_ALERT_EMAIL:
            _logger.warning("OPS_ALERT_EMAIL set but RESEND_API_KEY missing, e-mail route off")
        if settings.OPS_ALERT_WEBHOOK_URL:
            results["webhook"] = _route("webhook", _send_webhook, kind, key, subject, body)
        if results["email"] == "off" and results["webhook"] == "off":
            # WARNING, not a silent return: production once ran with OPS_ALERT_EMAIL blank
            # and every alert vanished without a trace (see the note in send_ops_alert).
            _logger.warning(
                "no ops alert route configured, alert dropped [%s:%s]: %s", kind, key, subject
            )
    except Exception as exc:  # noqa: BLE001 — never fail the caller
        _logger.error("ops alert routing failed [%s:%s]: %s", kind, key, str(exc)[:200])

    delivered = "ok" in results.values()
    outcome = "[{}] [{}]".format(
        "sent" if delivered else "undelivered",
        " ".join(f"{route}:{result}" for route, result in results.items()),
    )
    _persist_ops_alert(kind, key, subject, outcome)
    return delivered


def send_ops_alert(kind: str, key: str, subject: str, body: str) -> bool:
    """Send an operational alert on every configured route. Never raises.

    ``kind``/``key`` form the cooldown bucket (e.g. ("canary", "pierce/WA")).
    ``subject``/``body`` are plain text; body is HTML-escaped into the e-mail.

    Returns True when at least one route delivered. Called from a running event loop
    (the Stripe webhook handler in src/api/routes/billing.py), the whole send moves to
    the loop's default executor and this returns False at once: the outcome lands in
    the audit row, and no network or database call ever blocks the loop. Every other
    caller is a sync Celery worker and gets the real result.
    """
    # Every alert-worthy condition leaves a durable row, whether or not anything goes
    # out, because the delivery path is exactly what turned out to be broken:
    # OPS_ALERT_EMAIL was '' in production, so this function silently returned False for
    # all 15 call sites (canary, batch, billing, delivery, NTS crawl, registration,
    # skip-trace, webhook) — and the King NTS crawl then went barren for four consecutive
    # weeks with no alert, no log line and nothing in the database. By the time anyone
    # noticed, Railway's stdout retention no longer reached back and the outage could not
    # be explained. An alert nobody can reconstruct afterwards is not an alert.
    #
    # Known caveat (Codex, accepted): `run_in_executor(None, ...)` is not fully detached —
    # asyncio drains the default executor when the loop closes, so under `asyncio.run` a
    # slow send can still delay process exit after the coroutine returns. Acceptable for
    # the one loop-bound caller (a long-lived FastAPI request loop). A hard kill can still
    # lose the row; the alternative that survives that is a Celery task, which cannot be
    # trusted for an alert that may BE about the broker.
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # ordinary sync caller (every Celery worker call site)
        if loop is not None:
            loop.run_in_executor(None, _send_and_record, kind, key, subject, body)
            return False
        return _send_and_record(kind, key, subject, body)
    except Exception as exc:  # noqa: BLE001 — never fail the caller
        _logger.error("ops alert dispatch failed [%s:%s]: %s", kind, key, str(exc)[:200])
        return False
