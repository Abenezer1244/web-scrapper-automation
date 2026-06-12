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


def send_ops_alert(kind: str, key: str, subject: str, body: str) -> bool:
    """Send an operational alert email. Returns True when an email was sent.

    ``kind``/``key`` form the cooldown bucket (e.g. ("canary", "pierce/WA")).
    ``subject``/``body`` are plain text; body is HTML-escaped into the template.
    """
    try:
        if not settings.OPS_ALERT_EMAIL:
            return False  # alerting not configured — silent no-op
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
        return True
    except Exception as exc:  # noqa: BLE001 — never fail the caller
        _logger.error("ops alert send failed [%s:%s]: %s", kind, key, str(exc)[:200])
        return False
