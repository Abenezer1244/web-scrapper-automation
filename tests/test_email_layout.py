"""Transactional email: sender identity, layout safety, and copy correctness.

Nothing here touches the network. Resend is an external API, so the send call is
captured (pytest's builtin monkeypatch, no unittest.mock) and the REAL template
code, REAL settings and REAL plan catalog run underneath. Every assertion is
about output the code actually produced.

The three regressions these tests exist to prevent, all of which reached real
inboxes:
  * the From header had no display name, so Gmail showed "leads"
  * <h1> declared no color and rendered black on a near-black card
  * the trial email hardcoded "$79/mo" after billing moved Pro to $199
"""

import re

import resend

from src.config import settings
from src.config.constants import TRIAL_PERIOD_DAYS
from src.config.plans import plan_price_monthly, plan_records_limit
from src.utils import email_layout
from src.utils.email_layout import (
    BRAND,
    CARD_BG,
    ON_BRAND,
    build_payload,
    from_header,
    paragraph,
    render_email,
    reply_to,
)
from src.workers import delivery, onboarding_emails

EM_DASH = "—"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _collect_all_emails(monkeypatch) -> list[tuple[str, str, str, str]]:
    """Render every user-facing template. Returns (name, subject, html, text)."""
    out: list[tuple[str, str, str, str]] = []

    def _make(name: str):
        def _capture(email, subject, html_body, text_body):
            out.append((name, subject, html_body, text_body))
        return _capture

    cases = [
        ("welcome", lambda: onboarding_emails.send_welcome_email("a@example.com")),
        ("duplicate_signup",
         lambda: onboarding_emails.send_duplicate_signup_email("a@example.com")),
        ("day1_nudge", lambda: onboarding_emails.send_day1_nudge("a@example.com", 6)),
        ("day3_no_scraper",
         lambda: onboarding_emails.send_activation_reminder("a@example.com", False, False, 4)),
        ("day3_no_download",
         lambda: onboarding_emails.send_activation_reminder("a@example.com", True, False, 4)),
        ("trial_2d",
         lambda: onboarding_emails.send_trial_ending_email("a@example.com", 2)),
        ("trial_today",
         lambda: onboarding_emails.send_trial_ending_email("a@example.com", 1)),
    ]
    for name, run in cases:
        monkeypatch.setattr(onboarding_emails, "_send", _make(name))
        run()

    # Lead delivery has a pure builder, so no capture is needed at all.
    subject, html_body, text_body = delivery._build_lead_delivery_email(
        "King County Probate", 1284, "https://app.bridgeleads.io/d?token=t", "csv",
    )
    out.append(("lead_delivery", subject, html_body, text_body))

    # The remaining delivery templates send inline: capture at the SDK boundary
    # so the real payload (including From and Reply-To) is what gets inspected.
    sent: list[dict] = []
    monkeypatch.setattr(resend.Emails, "send", lambda payload: sent.append(payload))
    monkeypatch.setattr(settings, "RESEND_API_KEY", "test-key-not-used-for-network")
    delivery._send_payment_failed_email("a@example.com", 2)
    delivery.send_lockout_notification("a@example.com", 5, "203.0.113.9")
    delivery.send_password_reset_email(
        "a@example.com", f"{settings.FRONTEND_URL}/reset-password?token=x")
    # The verification email sends directly (it must RAISE on failure for the
    # dispatcher), so it is captured at the SDK boundary like the others.
    onboarding_emails.send_verification_email(
        "a@example.com", f"{settings.FRONTEND_URL}/verify-email?token=x")
    for payload in sent:
        out.append(("delivery_direct", payload["subject"], payload["html"], payload["text"]))

    return out


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _lin(r), _lin(g), _lin(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ─── Sender identity ────────────────────────────────────────────────────────

def test_from_header_carries_the_bridgeleads_display_name():
    """Gmail showed 'leads' because the From header was a bare address."""
    assert from_header() == "BridgeLeads <leads@bridgeleads.io>"


def test_from_header_preserves_the_verified_sending_address():
    """The display name is additive. The addr-spec must not change, or domain
    verification and DMARC alignment break."""
    assert f"<{settings.EMAIL_FROM}>" in from_header()


def test_from_header_honours_a_display_name_already_set_in_env(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_FROM", "Ops Team <ops@bridgeleads.io>")
    assert from_header() == "Ops Team <ops@bridgeleads.io>"


def test_from_header_quotes_a_display_name_that_needs_it(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_FROM_NAME", "BridgeLeads, Inc.")
    assert from_header() == '"BridgeLeads, Inc." <leads@bridgeleads.io>'


def test_from_header_passes_through_an_unparseable_sender(monkeypatch):
    """A malformed EMAIL_FROM must never be mangled into something worse."""
    monkeypatch.setattr(settings, "EMAIL_FROM", "not-an-address")
    assert from_header() == "not-an-address"


def test_reply_to_points_at_support():
    assert reply_to() == ["support@bridgeleads.io"]


def test_reply_to_omitted_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "")
    assert reply_to() == []
    payload = build_payload(to=["a@b.io"], subject="s", html_body="h", text_body="t")
    assert "reply_to" not in payload


def test_build_payload_shape():
    payload = build_payload(to=["a@b.io"], subject="s", html_body="h", text_body="t")
    assert payload["from"] == "BridgeLeads <leads@bridgeleads.io>"
    assert payload["reply_to"] == ["support@bridgeleads.io"]
    assert payload["to"] == ["a@b.io"]
    assert payload == {**payload, "subject": "s", "html": "h", "text": "t"}


def test_every_send_site_uses_the_shared_sender_identity(monkeypatch):
    """No template may build its own From header."""
    sent: list[dict] = []
    monkeypatch.setattr(resend.Emails, "send", lambda payload: sent.append(payload))
    monkeypatch.setattr(settings, "RESEND_API_KEY", "test-key-not-used-for-network")

    onboarding_emails.send_welcome_email("a@example.com")
    delivery._send_payment_failed_email("a@example.com", 1)
    delivery.send_lockout_notification("a@example.com", 5, "203.0.113.9")
    delivery.send_password_reset_email("a@example.com", "https://bridgeleads.io/r?t=x")
    delivery.deliver_job_email(
        job_id="j", scraper_name="S", record_count=1,
        download_url="https://app.bridgeleads.io/d", recipient_emails=["a@example.com"],
    )
    onboarding_emails.send_verification_email(
        "a@example.com", f"{settings.FRONTEND_URL}/verify-email?token=x")

    assert len(sent) == 6
    for payload in sent:
        assert payload["from"] == "BridgeLeads <leads@bridgeleads.io>"
        assert payload["reply_to"] == ["support@bridgeleads.io"]


# ─── Copy rules ─────────────────────────────────────────────────────────────

def test_no_em_dash_in_any_user_facing_email(monkeypatch):
    """Hard gate: zero U+2014 in any subject, HTML body or text body."""
    offenders = []
    for name, subject, html_body, text_body in _collect_all_emails(monkeypatch):
        for part, value in (("subject", subject), ("html", html_body), ("text", text_body)):
            if EM_DASH in value:
                offenders.append(f"{name}.{part}")
    assert offenders == [], f"em dash found in: {offenders}"


def test_no_em_dash_entity_forms(monkeypatch):
    """&mdash; and &#8212; render as an em dash too."""
    for name, subject, html_body, text_body in _collect_all_emails(monkeypatch):
        blob = f"{subject}{html_body}{text_body}"
        assert "&mdash;" not in blob, name
        assert "&#8212;" not in blob, name
        assert "&#x2014;" not in blob.lower(), name


def test_no_stale_leads_sender_name_in_copy(monkeypatch):
    """The wordmark and signature must read BridgeLeads, never 'leads'."""
    for name, _subject, html_body, _text in _collect_all_emails(monkeypatch):
        assert ">BridgeLeads<" in html_body, name


# ─── Layout safety (root-cause regressions) ─────────────────────────────────

def test_heading_declares_an_explicit_colour(monkeypatch):
    """THE bug from the screenshots: <h1> had no color and inherited from body,
    which Gmail does not propagate, so it rendered black on a black card."""
    for name, _s, html_body, _t in _collect_all_emails(monkeypatch):
        match = re.search(r"<h1[^>]*style=\"([^\"]*)\"", html_body)
        assert match, f"{name}: no styled <h1>"
        assert "color:" in match.group(1), f"{name}: <h1> has no explicit color"


def test_no_text_element_relies_on_inherited_colour(monkeypatch):
    """Every <p>, <h1> and <div> that holds copy carries its own color."""
    for name, _s, html_body, _t in _collect_all_emails(monkeypatch):
        for tag_open in re.findall(r"<(?:p|h1)\b[^>]*>", html_body):
            assert "color:" in tag_open, f"{name}: {tag_open[:70]} has no color"


def test_no_flexbox_or_grid(monkeypatch):
    """Outlook ignores both. The old numbered steps used display:flex."""
    for name, _s, html_body, _t in _collect_all_emails(monkeypatch):
        assert "display:flex" not in html_body.replace(" ", ""), name
        assert "display:grid" not in html_body.replace(" ", ""), name


def test_no_javascript_or_external_assets(monkeypatch):
    for name, _s, html_body, _t in _collect_all_emails(monkeypatch):
        assert "<script" not in html_body.lower(), name
        assert "javascript:" not in html_body.lower(), name
        assert "fonts.googleapis.com" not in html_body, name
        assert "<img" not in html_body.lower(), name


def test_mobile_media_query_only_overrides_horizontal_padding():
    """The shorthand would flatten every section's vertical rhythm to one value."""
    html_body = render_email(
        title="t", preheader="p", heading="h", blocks=[paragraph("x")],
    )
    block = re.search(r"@media only screen[^{]*\{(.*?)\n  \}", html_body, re.S)
    assert block, "mobile media query missing"
    for rule in re.findall(r"padding[^;:]*:", block.group(1)):
        assert rule.strip() != "padding:", "media query uses the padding shorthand"


def _kitchen_sink() -> str:
    """One email exercising every block helper, so class coverage is complete."""
    from src.utils.email_layout import bullets, callout, numbered_steps, stat
    return render_email(
        title="t", preheader="p", heading="h",
        blocks=[
            paragraph("body copy"),
            paragraph("muted copy", muted=True),
            numbered_steps([("Step title", "Step detail")]),
            callout("informational note"),
            callout("attention note", tone="warning"),
            stat("1,284", "Records found"),
            bullets(["one", "two"]),
        ],
        cta=("Do the thing", "https://app.bridgeleads.io/go"),
        cta_note="a note",
        footer_note="a footer note",
    )


def test_dark_mode_overrides_every_coloured_class():
    """A PARTIAL dark override is worse than none: it reproduces the original
    black-on-black bug in mirror image (dark card, light-mode dark body text).
    Every class that carries a colour inline must appear in the dark block."""
    html_body = _kitchen_sink()
    dark_block = re.search(
        r"@media \(prefers-color-scheme: dark\)[^{]*\{(.*?)\n  \}", html_body, re.S
    )
    assert dark_block, "dark-mode block missing"
    rules = dark_block.group(1)

    coloured = set()
    for tag in re.findall(r"<[a-z0-9]+\b[^>]*>", html_body):
        klass = re.search(r'class="(bl-[^"]+)"', tag)
        style = re.search(r'style="([^"]*)"', tag)
        if not klass or not style:
            continue
        if "color:" in style.group(1) or "background-color:" in style.group(1):
            coloured.update(klass.group(1).split())

    # The CTA cell is deliberately exempt: teal fill with white text clears AA on
    # either ground, so it is intentionally identical in both schemes.
    coloured.discard("bl-btn-cell")

    missing = sorted(c for c in coloured if f".{c}" not in rules)
    assert missing == [], f"classes with no dark-mode override: {missing}"


def test_dark_mode_pairs_meet_wcag_aa():
    assert _contrast(email_layout.DARK_TEXT, email_layout.DARK_CARD_BG) >= 4.5
    assert _contrast(email_layout.DARK_BODY_TEXT, email_layout.DARK_CARD_BG) >= 4.5
    assert _contrast(email_layout.DARK_MUTED_TEXT, email_layout.DARK_CARD_BG) >= 4.5
    assert _contrast(email_layout.DARK_BRAND, email_layout.DARK_CARD_BG) >= 4.5
    assert _contrast(email_layout.DARK_ACCENT_TEXT, email_layout.DARK_ACCENT_BG) >= 4.5
    assert _contrast(email_layout.DARK_WARN_TEXT, email_layout.DARK_WARN_BG) >= 4.5
    # The CTA is unchanged between schemes, so it must clear on the dark card too.
    assert _contrast(ON_BRAND, BRAND) >= 4.5


def test_outlook_button_padding_is_mso_scoped():
    """Outlook drops padding on an <a>. The mso block moves it to the cell, and
    must zero the <a> padding so the button is never double-padded."""
    html_body = _kitchen_sink()
    mso = re.search(r"<!--\[if mso\]>(.*?)<!\[endif\]-->", html_body, re.S)
    assert mso, "mso conditional block missing"
    assert ".bl-btn-cell" in mso.group(1)
    assert "padding: 0 !important" in mso.group(1)
    assert 'class="bl-btn-cell"' in html_body


def test_viewport_and_colour_scheme_declared():
    html_body = render_email(
        title="t", preheader="p", heading="h", blocks=[paragraph("x")],
    )
    assert 'name="viewport"' in html_body
    assert 'name="color-scheme"' in html_body
    assert 'name="supported-color-schemes"' in html_body


def test_layout_tables_are_marked_presentational():
    """Screen readers must not announce the layout scaffold as data tables."""
    html_body = render_email(
        title="t", preheader="p", heading="h", blocks=[paragraph("x")],
    )
    assert html_body.count("<table") == html_body.count('role="presentation"')


def test_cta_contrast_meets_wcag_aa():
    assert _contrast(ON_BRAND, BRAND) >= 4.5
    assert _contrast(email_layout.TEXT, CARD_BG) >= 4.5
    assert _contrast(email_layout.BODY_TEXT, CARD_BG) >= 4.5
    assert _contrast(email_layout.MUTED_TEXT, CARD_BG) >= 4.5
    assert _contrast(email_layout.ACCENT_TEXT, email_layout.ACCENT_BG) >= 4.5
    assert _contrast(email_layout.WARN_TEXT, email_layout.WARN_BG) >= 4.5


def test_user_supplied_values_are_escaped():
    """A scraper name is user-controlled and reaches the HTML body."""
    _subject, html_body, _text = delivery._build_lead_delivery_email(
        '<script>alert(1)</script>', 5, "https://app.bridgeleads.io/d?a=1&b=2", "csv",
    )
    assert "<script>alert(1)</script>" not in html_body
    assert "&lt;script&gt;" in html_body
    # The ampersand in the download URL must be escaped in the href attribute.
    assert "?a=1&amp;b=2" in html_body


def test_subject_strips_control_characters_from_user_input():
    """A subject is a header, not HTML. CR/LF in a user-controlled scraper name
    must never reach it."""
    _subject, _html, _text = delivery._build_lead_delivery_email(
        "Pierce\r\nBcc: attacker@evil.test", 5, "https://app.bridgeleads.io/d", "csv",
    )
    assert "\r" not in _subject and "\n" not in _subject
    assert "Pierce Bcc: attacker@evil.test" in _subject


def test_subject_bounds_a_very_long_scraper_name():
    long_name = "A" * 500
    subject, _html, _text = delivery._build_lead_delivery_email(
        long_name, 5, "https://app.bridgeleads.io/d", "csv",
    )
    assert len(subject) < 160
    assert "records" in subject  # the useful tail survives the truncation


def test_header_text_helper():
    from src.utils.email_layout import header_text
    assert header_text("a\r\nb") == "a b"
    assert header_text("a\tb\x00c") == "a b c"
    assert header_text("  spaced   out  ") == "spaced out"
    assert header_text("") == ""


def test_every_email_has_a_plain_text_part(monkeypatch):
    for name, subject, _html, text_body in _collect_all_emails(monkeypatch):
        assert subject.strip(), name
        assert len(text_body.strip()) > 40, name
        assert "<" not in text_body.replace("<", "", 0) or "</" not in text_body, name


def test_every_email_has_a_preheader(monkeypatch):
    for name, _s, html_body, _t in _collect_all_emails(monkeypatch):
        assert "max-height:0" in html_body, name


# ─── Product data comes from config, not literals ───────────────────────────

def test_trial_email_quotes_the_current_pro_price(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        onboarding_emails, "_send",
        lambda e, s, h, t: captured.update(subject=s, html=h, text=t),
    )
    onboarding_emails.send_trial_ending_email("a@example.com", 2)

    price = plan_price_monthly("pro")
    assert f"${price:,}/month" in captured["html"]
    assert f"${price:,}/month" in captured["text"]
    # The stale literal that shipped must not reappear.
    assert "$79" not in captured["html"]
    assert "$79" not in captured["text"]


def test_trial_email_states_the_real_post_trial_allowance(monkeypatch):
    """_expire_trials_impl downgrades to Starter, so the email must say Starter's
    actual record limit, and must NOT claim a county cap that is not enforced."""
    captured = {}
    monkeypatch.setattr(
        onboarding_emails, "_send",
        lambda e, s, h, t: captured.update(html=h, text=t),
    )
    onboarding_emails.send_trial_ending_email("a@example.com", 2)

    assert f"{plan_records_limit('starter'):,} records per month" in captured["html"]
    assert "county" not in captured["html"].lower()


def test_trial_email_days_are_dynamic(monkeypatch):
    seen = []
    monkeypatch.setattr(
        onboarding_emails, "_send", lambda e, s, h, t: seen.append((s, h)),
    )
    onboarding_emails.send_trial_ending_email("a@example.com", 2)
    onboarding_emails.send_trial_ending_email("a@example.com", 1)

    assert "ends in 2 days" in seen[0][0] and "ends in 2 days" in seen[0][1]
    assert "ends today" in seen[1][0] and "ends today" in seen[1][1]


def test_welcome_email_quotes_the_configured_trial_and_allowance(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        onboarding_emails, "_send",
        lambda e, s, h, t: captured.update(subject=s, html=h, text=t),
    )
    onboarding_emails.send_welcome_email("a@example.com")

    assert f"{TRIAL_PERIOD_DAYS} days" in captured["html"]
    assert f"{plan_records_limit('pro'):,} records per month" in captured["html"]
    assert "/scrapers/new" in captured["html"]
    assert "Set Up Your First Scraper" in captured["html"]


def test_welcome_email_does_not_hardcode_counties_or_record_types(monkeypatch):
    """Counties and record types differ per plan and per live connector, so the
    generic welcome must not tell every recipient to pick a specific one."""
    captured = {}
    monkeypatch.setattr(
        onboarding_emails, "_send", lambda e, s, h, t: captured.update(html=h),
    )
    onboarding_emails.send_welcome_email("a@example.com")

    for stale in ("Pierce", "King", "98%", "motivated sellers"):
        assert stale not in captured["html"], stale


def test_nudge_trial_days_are_dynamic(monkeypatch):
    seen = []
    monkeypatch.setattr(onboarding_emails, "_send", lambda e, s, h, t: seen.append(h))
    onboarding_emails.send_day1_nudge("a@example.com", 6)
    onboarding_emails.send_day1_nudge("a@example.com", 1)
    onboarding_emails.send_activation_reminder("a@example.com", True, False, 3)

    assert "6 days left" in seen[0]
    assert "1 day left" in seen[1]
    assert "3 days left" in seen[2]
    # The literals the old copy always printed regardless of account state.
    assert "6 more days" not in "".join(seen)
    assert "4 days left on your Pro trial" not in "".join(seen)


def test_activation_reminder_skips_an_activated_user(monkeypatch):
    seen = []
    monkeypatch.setattr(onboarding_emails, "_send", lambda e, s, h, t: seen.append(s))
    onboarding_emails.send_activation_reminder("a@example.com", True, True, 3)
    assert seen == []


def test_cta_urls_use_the_configured_frontend(monkeypatch):
    for name, _s, html_body, _t in _collect_all_emails(monkeypatch):
        for url in re.findall(r'href="(https?://[^"]+)"', html_body):
            assert url.startswith((settings.FRONTEND_URL, "https://app.bridgeleads.io")), \
                f"{name}: unexpected link {url}"
