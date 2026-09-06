"""Render every transactional email to HTML for visual inspection.

No new dependency and no network: it monkeypatches the module-level ``_send``
helpers to capture (subject, html, text) instead of calling Resend, then writes
one file per template.

    python scripts/preview_emails.py [--out DIR]

Open the files in a browser at 320/375/390/430px to check the mobile layout, and
in an email client for a real client-rendering pass. Nothing here sends mail.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.utils.email_layout import from_header, reply_to  # noqa: E402

_captured: list[tuple[str, str, str, str]] = []  # (slug, subject, html, text)


class _StubResend:
    """Stands in for the resend SDK so the delivery module's direct sends are captured."""

    api_key = "preview"

    class Emails:
        @staticmethod
        def send(payload: dict) -> dict:
            _captured.append((
                "delivery", payload["subject"], payload["html"], payload["text"],
            ))
            return {"id": "preview"}


def _render_all() -> None:
    from src.workers import delivery, onboarding_emails

    # Onboarding: capture through the shared _send.
    def _make(slug: str):
        def _fake(email: str, subject: str, html_body: str, text_body: str) -> None:
            _captured.append((slug, subject, html_body, text_body))
        return _fake

    cases = [
        ("welcome", lambda: onboarding_emails.send_welcome_email("preview@example.com")),
        ("duplicate-signup",
         lambda: onboarding_emails.send_duplicate_signup_email("preview@example.com")),
        ("day1-nudge",
         lambda: onboarding_emails.send_day1_nudge("preview@example.com", 6)),
        ("day3-no-scraper",
         lambda: onboarding_emails.send_activation_reminder(
             "preview@example.com", False, False, 4)),
        ("day3-no-download",
         lambda: onboarding_emails.send_activation_reminder(
             "preview@example.com", True, False, 4)),
        ("trial-ending-2d",
         lambda: onboarding_emails.send_trial_ending_email("preview@example.com", 2)),
        ("trial-ending-today",
         lambda: onboarding_emails.send_trial_ending_email("preview@example.com", 1)),
    ]
    for slug, run in cases:
        onboarding_emails._send = _make(slug)
        run()

    # Delivery: the lead email has a pure builder; the rest send directly.
    subject, html_body, text_body = delivery._build_lead_delivery_email(
        "King County Probate", 1284,
        "https://app.bridgeleads.io/jobs/abc123/download?token=preview", "csv",
    )
    _captured.append(("lead-delivery", subject, html_body, text_body))

    delivery.resend = _StubResend()
    from src.config import settings
    settings.RESEND_API_KEY = settings.RESEND_API_KEY or "preview"
    delivery._send_payment_failed_email("preview@example.com", 2)
    delivery.send_lockout_notification("preview@example.com", 5, "203.0.113.9")
    delivery.send_password_reset_email(
        "preview@example.com", "https://bridgeleads.io/reset?token=preview")

    # The verification email sends directly (it must raise for the dispatcher),
    # so it is captured at the SDK boundary rather than through _send.
    onboarding_emails.resend = _StubResend()
    onboarding_emails.send_verification_email(
        "preview@example.com", "https://bridgeleads.io/verify-email?token=preview")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="logs/email-preview")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    _render_all()

    seen: dict[str, int] = {}
    index_rows = []
    for slug, subject, html_body, text_body in _captured:
        seen[slug] = seen.get(slug, 0) + 1
        name = slug if seen[slug] == 1 else f"{slug}-{seen[slug]}"
        (out / f"{name}.html").write_text(html_body, encoding="utf-8")
        (out / f"{name}.txt").write_text(text_body, encoding="utf-8")
        index_rows.append(f'<li><a href="{name}.html">{name}</a>: {subject}</li>')

    (out / "index.html").write_text(
        "<!DOCTYPE html><meta charset='utf-8'><title>BridgeLeads email previews</title>"
        "<body style='font-family:system-ui;padding:24px;line-height:1.7'>"
        f"<h1>BridgeLeads email previews</h1><p><b>From:</b> {from_header()}<br>"
        f"<b>Reply-To:</b> {', '.join(reply_to()) or '(none)'}</p><ul>"
        + "".join(index_rows) + "</ul>",
        encoding="utf-8",
    )

    print(f"From:     {from_header()}")
    print(f"Reply-To: {', '.join(reply_to()) or '(none)'}")
    print(f"Rendered {len(_captured)} templates to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
