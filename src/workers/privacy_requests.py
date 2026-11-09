"""Privacy requests from the web form: the outbox email and the deadline alerts (P8d).

send_privacy_outbox (beat, every minute)
  One row at a time, each in its own transaction (migration 137's privacy_outbox_due
  locks it FOR UPDATE SKIP LOCKED): decrypt the contact, mint the link's token, send,
  then store only the token's hash and mark the row sent. A send failure backs off
  (privacy_outbox_failed); a crash after sending and before the commit sends again
  (at-least-once; the earlier link then never matches a stored hash, so only the
  newest works).
  Copy (D9: drafted by engineering, for counsel): what was received and when, the
  response date, one-time link. Never a submitted name, address or match result, and
  never a claim that anything was done.

privacy_request_deadlines (beat, daily)
  Ops alert listing open requests due within 7 days (ids, kinds, dates only); purges
  tokens expired for 30 days.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import resend
from sqlalchemy import text

from src.config import settings
from src.db.session import system_sync_session
from src.utils.crypto import decrypt_field
from src.utils.email_layout import build_payload, paragraph, render_email, text_footer
from src.utils.logger import setup_logger

_logger = setup_logger("worker.privacy_requests")

_TZ = ZoneInfo("America/Los_Angeles")
_LINK_TTL = timedelta(days=7)
_PER_TICK = 50
_ALERT_WINDOW = "7 days"

_ASKED = {
    "opt_out": "opt out of the sale or sharing of your personal information",
    "delete": "delete your personal information",
    "access": "know what personal information we hold about you",
    "correct": "correct your personal information",
}
_IGNORE = ("If you did not make this request, you can ignore this email; nothing "
           "happens without the confirmation.")


def _day(ts: datetime) -> str:
    d = ts.astimezone(_TZ)
    return f"{d:%B} {d.day}, {d.year}"


def _copy(row, link: str | None) -> tuple[str, list[str], tuple[str, str] | None, str]:
    """(subject, paragraphs, call to action, footer note) for one outbox row."""
    asked = _ASKED[row.request_kind]
    received, due = _day(row.received_at), _day(row.deadline_at)
    if row.email_kind == "receipt":
        return (
            "We received your privacy request",
            [f"We received a request you submitted on someone else's behalf on {received} "
             f"to {asked}.",
             "We will review the authorization you described before acting on it, and we "
             "may contact you for the signed permission.",
             f"We will respond by {due}."],
            None,
            f"Questions: {settings.SUPPORT_EMAIL}.",
        )
    return (
        "Confirm your privacy request",
        [f"We received a request on {received} to {asked}.",
         "Please confirm it was you. The link works once and expires in 7 days.",
         f"We will respond by {due}. The request stays open while we verify it."],
        ("Confirm my request", link),
        _IGNORE,
    )


def _send(to: str, subject: str, lines: list[str], cta, note: str) -> None:
    html_body = render_email(
        title=subject, preheader=lines[0], heading=subject,
        blocks=[paragraph(line) for line in lines], cta=cta, footer_note=note)
    text_body = "\n\n".join(lines + ([f"{cta[0]}: {cta[1]}"] if cta else []))
    resend.Emails.send(build_payload(
        to=[to], subject=subject, html_body=html_body,
        text_body=text_body + "\n\n" + text_footer(footer_note=note)))


def _send_one(db) -> bool:
    """Send the next due row; False when none is due."""
    row = db.execute(text("SELECT * FROM public.privacy_outbox_due(1)")).first()
    if row is None:
        return False
    contact = json.loads(decrypt_field(row.contact_encrypted))
    to = (contact.get("agent") or {}).get("email") or contact["email"]
    token = link = None
    if row.email_kind == "verification":
        token = secrets.token_urlsafe(32)
        link = f"{settings.FRONTEND_URL.rstrip('/')}/privacy/request/confirm#t={token}"
    subject, lines, cta, note = _copy(row, link)
    try:
        _send(to, subject, lines, cta, note)
    except Exception as exc:  # noqa: BLE001 -- recorded and retried with backoff
        from src.workers.delivery import _email_error_summary
        db.execute(text("SELECT public.privacy_outbox_failed(:o, :e)"),
                   {"o": row.outbox_id, "e": _email_error_summary(exc)[:200]})
        db.commit()
        _logger.warning("privacy outbox row %s: send failed", str(row.outbox_id)[:8])
        return True
    db.execute(text("SELECT public.privacy_outbox_sent(:o, :h, :x)"), {
        "o": row.outbox_id,
        "h": hashlib.sha256(token.encode()).hexdigest() if token else None,
        "x": datetime.now(UTC) + _LINK_TTL if token else None,
    })
    db.commit()
    return True


def send_privacy_outbox(*, limit: int = _PER_TICK) -> int:
    """Rows handled this tick (sent or backed off)."""
    if not settings.RESEND_API_KEY:
        return 0  # rows stay queued; nothing is lost
    handled = 0
    with system_sync_session() as db:
        while handled < limit and _send_one(db):
            handled += 1
    return handled


def privacy_request_deadlines() -> dict:
    from src.workers.ops_alerts import send_ops_alert

    with system_sync_session() as db:
        due = db.execute(text("SELECT * FROM public.privacy_requests_due(CAST(:w AS interval))"),
                         {"w": _ALERT_WINDOW}).all()
        purged = db.execute(text("SELECT public.privacy_tokens_purge()")).scalar() or 0
        db.commit()
    if due:
        lines = [f"{r.request_id} {r.kind} due {_day(r.deadline_at)}" for r in due]
        send_ops_alert(
            "privacy_deadline", datetime.now(UTC).strftime("%Y-%m-%d"),
            f"{len(due)} privacy request(s) due within 7 days",
            "Open privacy requests (decide them with scripts/privacy_admin.py):\n"
            + "\n".join(lines))
    return {"due": len(due), "purged_tokens": purged}


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.privacy_requests.send_privacy_outbox")
    def send_privacy_outbox_task() -> int:
        return send_privacy_outbox()

    @app.task(name="src.workers.privacy_requests.privacy_request_deadlines")
    def privacy_request_deadlines_task() -> dict:
        return privacy_request_deadlines()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.error("Privacy request tasks NOT registered: %s", str(exc)[:120])
