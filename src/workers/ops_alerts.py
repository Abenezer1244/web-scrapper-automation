"""M6: operational alerting for watchdog/canary/batch failures (audit item).

Before this module those failures were LOG-ONLY — nobody gets paged when a
scraper portal goes down or a job dies permanently. Alerts go to
``settings.OPS_ALERT_EMAIL`` via Resend (already integrated for delivery), so
there is no new dependency or webhook surface.

Design rules:
- **Best-effort, never raises**: an alerting failure must never fail the
  watchdog/canary/sweep that called it. Every path is caught and logged.
- **Cooldown** per (kind, key) in Redis (SET NX EX) so a connector that is
  down all day emails once per ``OPS_ALERT_COOLDOWN_SECONDS``, not once per
  canary tick. Redis unavailable -> SEND anyway (noisy beats silent for ops).
- **Disabled by default**: empty OPS_ALERT_EMAIL = no-op (dev/CI safe).
- No PII in alert bodies: job/connector identifiers only, never lead data.
"""
import html

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.ops_alerts")

_COOLDOWN_PREFIX = "ops_alert:"


def _cooldown_acquired(kind: str, key: str) -> bool:
    """True when this (kind, key) is NOT in cooldown (and marks it). Fails open."""
    try:
        import redis as sync_redis

        r = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        try:
            return bool(
                r.set(
                    f"{_COOLDOWN_PREFIX}{kind}:{key}",
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


def _persist_ops_alert(kind: str, key: str, subject: str, delivered: bool) -> None:
    """Best-effort durable record of an ops alert. NEVER raises.

    Written to audit_events (event='ops_alert', user_id NULL) rather than to a new
    table: system-written rows with a NULL user_id are already an established pattern
    there, so this needs no migration. Deliberately records EVERY alert-worthy
    occurrence, not just the ones that clear the e-mail cooldown — "this fired daily
    for four weeks" is the shape of the question these rows exist to answer.

    created_at is set client-side so the INSERT emits no RETURNING: under FORCE RLS the
    app role may INSERT into audit_events but not SELECT, and a server_default would
    trigger a RETURNING that RLS then denies (same trap as _persist_audit_event in
    src/api/middleware/security.py).
    """
    try:
        import uuid as _uuid
        from datetime import UTC, datetime

        from src.db.models import AuditEvent
        from src.db.session import system_sync_session

        with system_sync_session() as db:
            db.add(
                AuditEvent(
                    id=str(_uuid.uuid4()),
                    event="ops_alert",
                    user_id=None,
                    path=f"{kind}:{key}"[:256],
                    # The outcome is part of the record: "fired daily for four weeks
                    # and was never delivered" is a different fact from "fired once and
                    # was e-mailed", and only the row can tell them apart later.
                    detail=f"[{'sent' if delivered else 'undelivered'}] {subject}"[:512],
                    created_at=datetime.now(UTC),
                )
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 — durability is additive, never fatal
        _logger.warning("ops_alert audit insert failed (alert still logged): %s", str(exc)[:200])


def send_ops_alert(kind: str, key: str, subject: str, body: str) -> bool:
    """Send an operational alert email. Returns True when an email was sent.

    ``kind``/``key`` form the cooldown bucket (e.g. ("canary", "pierce/WA")).
    ``subject``/``body`` are plain text; body is HTML-escaped into the template.
    """
    # Every alert-worthy condition leaves a durable row, whether or not an email goes
    # out, because the delivery path is exactly what turned out to be broken:
    # OPS_ALERT_EMAIL was '' in production, so this function silently returned False for
    # all 15 call sites (canary, batch, billing, delivery, NTS crawl, registration,
    # skip-trace, webhook) — and the King NTS crawl then went barren for four consecutive
    # weeks with no alert, no log line and nothing in the database. By the time anyone
    # noticed, Railway's stdout retention no longer reached back and the outage could not
    # be explained. An alert nobody can reconstruct afterwards is not an alert.
    #
    # Recorded in a `finally`, AFTER delivery, deliberately. Persisting first would put a
    # database write in front of every alert e-mail, so a slow or unreachable database
    # would delay the very message reporting that something is wrong. `finally` keeps the
    # recording unconditional — every gate below returns through it — without letting the
    # database sit on the critical path.
    delivered = False
    try:
        if not settings.OPS_ALERT_EMAIL:
            # WARNING, not a silent return. The RESEND_API_KEY branch below already
            # warned; the far likelier misconfiguration was the one that said nothing.
            _logger.warning(
                "OPS_ALERT_EMAIL not configured — alert dropped [%s:%s]: %s", kind, key, subject
            )
            return False
        if not settings.RESEND_API_KEY:
            _logger.warning("OPS_ALERT_EMAIL set but RESEND_API_KEY missing — alert dropped: %s", subject)
            return False
        if not _cooldown_acquired(kind, key):
            _logger.info("ops alert suppressed (cooldown) [%s:%s] %s", kind, key, subject)
            return False

        import resend

        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM,
                "to": [settings.OPS_ALERT_EMAIL],
                "subject": f"[BridgeLeads OPS] {subject}",
                "html": (
                    "<div style='font-family:monospace;font-size:13px'>"
                    f"<p><b>{html.escape(subject)}</b></p>"
                    f"<pre style='white-space:pre-wrap'>{html.escape(body)}</pre>"
                    f"<p style='color:#888'>kind={html.escape(kind)} key={html.escape(key)} · "
                    f"cooldown {settings.OPS_ALERT_COOLDOWN_SECONDS}s</p>"
                    "</div>"
                ),
            }
        )
        _logger.info("ops alert sent [%s:%s] %s", kind, key, subject)
        delivered = True
        return True
    except Exception as exc:  # noqa: BLE001 — never fail the caller
        _logger.error("ops alert send failed [%s:%s]: %s", kind, key, str(exc)[:200])
        return False
    finally:
        _persist_ops_alert(kind, key, subject, delivered)
