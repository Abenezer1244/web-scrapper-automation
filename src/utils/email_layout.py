"""Shared transactional email foundation: sender identity + HTML shell.

Every user-facing BridgeLeads email is built here. Two problems this module
exists to fix, both of which shipped to real inboxes:

1. SENDER IDENTITY. ``settings.EMAIL_FROM`` is a bare address, so the From
   header carried no display name and Gmail rendered the address local part:
   recipients saw "leads". ``from_header()`` is now the ONLY place a From header
   is composed, so the display name cannot drift per template. Reply-To is a
   separate header (support inbox) from the deliverability-verified From
   address, so pointing replies somewhere friendly never touches SPF/DKIM.

2. UNREADABLE HEADINGS. The old templates styled ``<h1>`` with size/weight but
   NO color, relying on inheriting ``color`` from ``<body>`` via a ``<style>``
   block. Gmail does not propagate that inherited color to block children, so
   the heading fell back to Gmail's near-black default on top of a near-black
   card. Every element that rendered correctly had an EXPLICIT color; the one
   that did not was the one that broke.

   The rules that follow from that, and that this module enforces:
     * Every text element carries an explicit INLINE color. No element may rely
       on inheritance or on a <style> block for its color.
     * The surface is light and neutral. A light ground survives a client's
       dark-mode transform far more predictably than a near-black card, and a
       failed transform degrades to dark-on-light rather than black-on-black.
     * Layout is nested tables with role="presentation". No flexbox or grid
       (the old numbered steps used display:flex, which Outlook ignores
       entirely, collapsing the step markers onto the text).
     * The <style> block carries ONLY progressive enhancement (mobile padding,
       prefers-color-scheme). Nothing the email needs to be readable lives
       there, because Gmail strips it in several contexts.
     * No web fonts, no JavaScript, no external images.

Copy rules: no em dashes anywhere in user-facing output (see
tests/test_email_layout.py, which fails the build if one appears), no emoji, no
marketing superlatives.
"""

from __future__ import annotations

import html
from email.utils import formataddr, parseaddr

from src.config import settings

# ─── Palette ────────────────────────────────────────────────────────────────
# Sourced from design-system/bridgeleads/MASTER.md. Contrast ratios below are
# against the surface each token is used on; all clear WCAG AA (4.5:1).
BRAND = "#0F766E"          # primary teal. white-on-brand = 5.5:1
BRAND_DARK = "#115E59"     # button border/hover, deeper teal
PAGE_BG = "#F1F5F9"        # page ground behind the card
CARD_BG = "#FFFFFF"        # card surface
CARD_BORDER = "#E2E8F0"
TEXT = "#111827"           # headings.       on card = 16.9:1
BODY_TEXT = "#374151"      # paragraphs.     on card = 10.4:1
MUTED_TEXT = "#6B7280"     # footer/meta.    on card =  4.8:1
ON_BRAND = "#FFFFFF"       # text on the brand button
ACCENT_BG = "#F0FDFA"      # informational callout surface
ACCENT_BORDER = "#99F6E4"
ACCENT_TEXT = "#134E4A"    # on ACCENT_BG = 9.0:1
WARN_BG = "#FFFBEB"        # attention callout surface
WARN_BORDER = "#FCD34D"
WARN_TEXT = "#78350F"      # on WARN_BG = 8.9:1

# Dark counterparts, applied under prefers-color-scheme by the shell.
#
# These MUST cover every token above that lands on the card. A PARTIAL dark
# override is worse than none: the first version of this module darkened only
# the card and the heading, which would have left #374151 body copy on a
# #1E293B card. That is the exact black-on-black failure this whole change
# exists to fix, in mirror image. test_dark_mode_overrides_every_coloured_class
# fails the build if a new coloured class is added without a dark value.
DARK_PAGE_BG = "#0F172A"
DARK_CARD_BG = "#1E293B"        # every ratio below is against this surface
DARK_CARD_BORDER = "#334155"
DARK_TEXT = "#F8FAFC"           # headings.    15.9:1
DARK_BODY_TEXT = "#CBD5E1"      # paragraphs.   9.9:1
DARK_MUTED_TEXT = "#94A3B8"     # footer/meta.  5.7:1
DARK_BRAND = "#5EEAD4"          # wordmark/links/figures. 9.9:1
DARK_ACCENT_BG = "#134E4A"
DARK_ACCENT_BORDER = "#115E59"
DARK_ACCENT_TEXT = "#99F6E4"    # on DARK_ACCENT_BG = 7.6:1
DARK_WARN_BG = "#422006"
DARK_WARN_BORDER = "#78350F"
DARK_WARN_TEXT = "#FDE68A"      # on DARK_WARN_BG = 11.7:1

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)

# Card width. 600px is the widest that renders without horizontal scroll in the
# Outlook desktop reading pane; it shrinks fluidly below that on mobile.
CARD_WIDTH = 600


# ─── Sender identity ────────────────────────────────────────────────────────

def from_header() -> str:
    """The RFC 5322 From header, e.g. ``BridgeLeads <leads@bridgeleads.io>``.

    The address is ``settings.EMAIL_FROM`` unchanged (it is the verified Resend
    sender; changing it would break domain verification and DMARC alignment).
    Only the display name is added.

    If EMAIL_FROM is already configured WITH a display name, that wins and is
    returned untouched, so an operator can override the name from the
    environment without a deploy. If it cannot be parsed as an address at all we
    return it verbatim rather than risk mangling a working sender.
    """
    raw = (settings.EMAIL_FROM or "").strip()
    existing_name, address = parseaddr(raw)
    # parseaddr is lenient: it hands back any bare token as an "address", so
    # emptiness alone is not a good enough check. Require something addr-spec
    # shaped before wrapping it in a display name; anything else passes through
    # untouched rather than being decorated into a different malformed value.
    if not address or "@" not in address:
        return raw
    if existing_name:
        return raw  # operator already set a display name at the env layer
    name = (settings.EMAIL_FROM_NAME or "").strip()
    if not name:
        return address
    # formataddr handles RFC 5322 quoting and RFC 2047 encoding for us.
    return formataddr((name, address))


def header_text(value: str, *, limit: int = 160) -> str:
    """Make a user-supplied string safe to place in a HEADER field (a subject).

    html.escape() protects the BODY, but a subject is not HTML: it is a header,
    and CR/LF there is a header-injection vector. Resend is a JSON API rather
    than raw SMTP, so this is defence in depth rather than a live exploit, but
    ``ScraperConfig.name`` is user input that reaches the delivery subject and
    nothing else strips control characters from it.

    Newlines and other C0/C1 control characters collapse to a single space, runs
    of whitespace collapse, and the result is bounded so one long scraper name
    cannot push the useful part of a subject out of the inbox preview.
    """
    cleaned = "".join(
        " " if (ch < " " or ch == "\x7f" or "\x80" <= ch <= "\x9f") else ch
        for ch in (value or "")
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def reply_to() -> list[str]:
    """Reply-To recipients, or an empty list when none is configured.

    Empty means the header is omitted entirely and replies fall back to the From
    address, which is the pre-existing behavior.
    """
    value = (settings.EMAIL_REPLY_TO or "").strip()
    return [value] if value else []


def support_email() -> str:
    """Support address shown in footers. Falls back to the From address."""
    return (settings.SUPPORT_EMAIL or "").strip() or settings.EMAIL_FROM


def build_payload(
    *,
    to: list[str],
    subject: str,
    html_body: str,
    text_body: str,
) -> dict:
    """Assemble the Resend send payload with the shared sender identity.

    Every ``resend.Emails.send()`` call site builds its dict here so the From
    display name and Reply-To can never be set (or forgotten) per template.
    """
    payload: dict = {
        "from": from_header(),
        "to": to,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    replies = reply_to()
    if replies:
        payload["reply_to"] = replies
    return payload


# ─── Content blocks ─────────────────────────────────────────────────────────
# Each helper returns a self-contained HTML fragment with explicit inline colors
# and escapes its own text arguments.

def paragraph(text: str, *, muted: bool = False) -> str:
    """A body paragraph. ``muted`` renders supporting/secondary detail."""
    color, size, klass = (
        (MUTED_TEXT, "14px", "bl-muted") if muted else (BODY_TEXT, "16px", "bl-text")
    )
    return (
        f'<p class="{klass}" style="margin:0 0 16px;padding:0;color:{color};'
        f'font-family:{FONT_STACK};font-size:{size};line-height:1.6;">'
        f"{html.escape(text)}</p>"
    )


def numbered_steps(steps: list[tuple[str, str]]) -> str:
    """A numbered how-to list as ``[(title, detail), ...]``.

    Table based on purpose: the previous version used ``display:flex``, which
    Outlook drops, stacking the number badge above its own text. The number is
    real text, not an image or a color cue, so it survives image blocking.
    """
    rows = []
    for index, (title, detail) in enumerate(steps, start=1):
        rows.append(
            '<tr>'
            f'<td width="32" valign="top" style="padding:0 12px 16px 0;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td align="center" valign="middle" width="26" height="26" '
            f'bgcolor="{BRAND}" style="background-color:{BRAND};border-radius:13px;'
            f'color:{ON_BRAND};font-family:{FONT_STACK};font-size:13px;'
            f'font-weight:700;line-height:26px;">{index}</td></tr>'
            '</table></td>'
            f'<td valign="top" style="padding:0 0 16px;">'
            f'<div class="bl-strong" style="margin:0 0 2px;color:{TEXT};'
            f'font-family:{FONT_STACK};font-size:15px;font-weight:600;'
            f'line-height:1.4;">{html.escape(title)}</div>'
            f'<div class="bl-text" style="margin:0;color:{BODY_TEXT};'
            f'font-family:{FONT_STACK};font-size:14px;line-height:1.6;">'
            f"{html.escape(detail)}</div>"
            '</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="margin:0 0 8px;">' + "".join(rows) + "</table>"
    )


def callout(text: str, *, tone: str = "brand") -> str:
    """A boxed note. ``tone`` is "brand" (informational) or "warning".

    The tone is carried by text and surface contrast, never by color alone, so
    the note is still legible to a reader who cannot distinguish the two.
    """
    if tone == "warning":
        bg, border, color, klass = WARN_BG, WARN_BORDER, WARN_TEXT, "bl-warn"
    else:
        bg, border, color, klass = ACCENT_BG, ACCENT_BORDER, ACCENT_TEXT, "bl-note"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="margin:0 0 20px;"><tr>'
        f'<td class="{klass}" bgcolor="{bg}" style="background-color:{bg};'
        f'border:1px solid {border};border-radius:8px;padding:14px 16px;'
        f'color:{color};font-family:{FONT_STACK};font-size:14px;line-height:1.6;">'
        f"{html.escape(text)}</td></tr></table>"
    )


def stat(value: str, label: str) -> str:
    """A single large figure with its label underneath."""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="margin:0 0 24px;"><tr>'
        f'<td class="bl-note" bgcolor="{ACCENT_BG}" '
        f'style="background-color:{ACCENT_BG};border:1px solid {ACCENT_BORDER};'
        f'border-radius:8px;padding:18px 20px;">'
        f'<div class="bl-figure" style="margin:0;color:{BRAND};'
        f'font-family:{FONT_STACK};font-size:32px;font-weight:700;'
        f'line-height:1.1;">{html.escape(value)}</div>'
        f'<div class="bl-note-label" style="margin:4px 0 0;color:{ACCENT_TEXT};'
        f'font-family:{FONT_STACK};font-size:12px;font-weight:600;'
        f'letter-spacing:0.04em;text-transform:uppercase;">'
        f"{html.escape(label)}</div>"
        f"</td></tr></table>"
    )


def bullets(items: list[str]) -> str:
    """A plain list of short benefit/detail lines."""
    rows = "".join(
        '<tr>'
        f'<td class="bl-figure" width="16" valign="top" '
        f'style="padding:0 8px 8px 0;color:{BRAND};font-family:{FONT_STACK};'
        f'font-size:15px;line-height:1.6;">&bull;</td>'
        f'<td class="bl-text" valign="top" style="padding:0 0 8px;'
        f'color:{BODY_TEXT};font-family:{FONT_STACK};font-size:15px;'
        f'line-height:1.6;">{html.escape(item)}</td></tr>'
        for item in items
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="margin:0 0 20px;">' + rows + "</table>"
    )


def _button(label: str, url: str) -> str:
    """Bulletproof CTA.

    The cell carries the fill so Outlook paints a real button rather than a bare
    link. The padding lives on the <a> so the tap target is ~47px tall on touch
    clients, comfortably over the 44px guideline; Outlook's Word engine drops
    padding on an <a>, so the mso-only style block in the shell moves that same
    padding onto .bl-btn-cell there instead. One of the two always applies, never
    both, so the button is never double-padded.
    """
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:8px 0 24px;"><tr>'
        f'<td class="bl-btn-cell" align="center" bgcolor="{BRAND}" '
        f'style="background-color:{BRAND};border:1px solid {BRAND_DARK};'
        f'border-radius:8px;">'
        f'<a href="{html.escape(url, quote=True)}" '
        f'style="display:inline-block;padding:14px 28px;color:{ON_BRAND};'
        f'font-family:{FONT_STACK};font-size:16px;font-weight:600;'
        f'line-height:1.2;text-decoration:none;">{html.escape(label)}</a>'
        f"</td></tr></table>"
    )


# ─── Shell ──────────────────────────────────────────────────────────────────

def render_email(
    *,
    title: str,
    preheader: str,
    heading: str,
    blocks: list[str],
    cta: tuple[str, str] | None = None,
    cta_note: str | None = None,
    footer_note: str | None = None,
    show_support: bool = True,
) -> str:
    """Render one transactional email.

    ``blocks`` are fragments from the helpers above (already escaped). ``title``,
    ``preheader``, ``heading``, ``cta_note`` and ``footer_note`` are plain text
    and are escaped here. ``cta`` is ``(label, url)``.
    """
    safe_support = html.escape(support_email())
    body_html = "".join(blocks)

    cta_html = _button(*cta) if cta else ""
    cta_note_html = paragraph(cta_note, muted=True) if cta_note else ""

    support_html = ""
    if show_support:
        support_html = (
            f'<p class="bl-text" style="margin:0 0 8px;color:{BODY_TEXT};'
            f'font-family:{FONT_STACK};font-size:14px;line-height:1.6;">'
            f'Questions? Reply to this email or contact '
            f'<a class="bl-link" href="mailto:{safe_support}" style="color:{BRAND};'
            f'text-decoration:underline;word-break:break-word;">{safe_support}</a>.'
            "</p>"
        )

    footer_note_html = ""
    if footer_note:
        footer_note_html = (
            f'<p class="bl-muted" style="margin:0 0 8px;color:{MUTED_TEXT};'
            f'font-family:{FONT_STACK};font-size:12px;line-height:1.6;">'
            f"{html.escape(footer_note)}</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{html.escape(title)}</title>
<!--[if mso]>
<style>
  /* Outlook's Word engine ignores padding on an <a>, which would collapse the
     CTA to bare text on a coloured strip. Move the same padding onto the cell
     there. Scoped to mso so no other client sees it. */
  .bl-btn-cell {{ padding: 14px 28px !important; }}
  .bl-btn-cell a {{ padding: 0 !important; }}
</style>
<![endif]-->
<style>
  /* Progressive enhancement only. Nothing required for readability lives here,
     because Gmail strips this block in several contexts. */
  @media only screen and (max-width: 600px) {{
    /* HORIZONTAL padding only. The shorthand would also overwrite each cell's
       vertical padding, flattening the section rhythm into a uniform gap. */
    .bl-shell {{ padding-left: 8px !important; padding-right: 8px !important; }}
    .bl-pad {{ padding-left: 24px !important; padding-right: 24px !important; }}
    .bl-h1 {{ font-size: 22px !important; }}
  }}
  @media (prefers-color-scheme: dark) {{
    /* Deliberate dark values so a client that honors the query does not invent
       its own pairing. Clients that ignore it keep the light design, which is
       already fully specified inline.

       This must cover EVERY coloured class. A partial override (dark card,
       light-mode body copy) reproduces the black-on-black bug this module was
       written to fix. !important is required because the light values are
       inline and would otherwise win. */
    /* body too, not just the page table: the table only covers its own height,
       so without this the ground below the card stays light. */
    .bl-body, .bl-page {{ background-color: {DARK_PAGE_BG} !important; }}
    .bl-card {{ background-color: {DARK_CARD_BG} !important;
                border-color: {DARK_CARD_BORDER} !important; }}
    .bl-rule {{ background-color: {DARK_CARD_BORDER} !important; }}
    .bl-h1, .bl-strong {{ color: {DARK_TEXT} !important; }}
    .bl-wordmark, .bl-figure, .bl-link {{ color: {DARK_BRAND} !important; }}
    .bl-text {{ color: {DARK_BODY_TEXT} !important; }}
    .bl-muted, .bl-foot {{ color: {DARK_MUTED_TEXT} !important; }}
    .bl-note {{ background-color: {DARK_ACCENT_BG} !important;
                border-color: {DARK_ACCENT_BORDER} !important;
                color: {DARK_ACCENT_TEXT} !important; }}
    .bl-note-label {{ color: {DARK_ACCENT_TEXT} !important; }}
    .bl-warn {{ background-color: {DARK_WARN_BG} !important;
                border-color: {DARK_WARN_BORDER} !important;
                color: {DARK_WARN_TEXT} !important; }}
    /* The CTA keeps its teal fill and white text: 5.5:1 on either ground. */
  }}
</style>
</head>
<body class="bl-body" style="margin:0;padding:0;background-color:{PAGE_BG};
  -webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
<div style="display:none;font-size:1px;color:{PAGE_BG};line-height:1px;
  max-height:0;max-width:0;opacity:0;overflow:hidden;">{html.escape(preheader)}</div>
<table role="presentation" class="bl-page" width="100%" cellpadding="0" cellspacing="0"
  border="0" bgcolor="{PAGE_BG}" style="background-color:{PAGE_BG};width:100%;">
<tr>
<td align="center" class="bl-shell" style="padding:32px 16px;">

  <table role="presentation" class="bl-card" width="{CARD_WIDTH}" cellpadding="0"
    cellspacing="0" border="0" bgcolor="{CARD_BG}"
    style="width:100%;max-width:{CARD_WIDTH}px;background-color:{CARD_BG};
    border:1px solid {CARD_BORDER};border-radius:12px;">

    <tr><td class="bl-pad" style="padding:32px 40px 0;"><div class="bl-wordmark" style="margin:0 0 24px;color:{BRAND};font-family:{FONT_STACK};font-size:18px;font-weight:700;letter-spacing:-0.01em;line-height:1.2;">BridgeLeads</div><h1 class="bl-h1" style="margin:0 0 20px;color:{TEXT};font-family:{FONT_STACK};font-size:24px;font-weight:600;line-height:1.3;">{html.escape(heading)}</h1></td></tr>

    <tr><td class="bl-pad" style="padding:0 40px;">{body_html}{cta_html}{cta_note_html}</td></tr>

    <tr><td class="bl-pad" style="padding:8px 40px 0;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td class="bl-rule" height="1" bgcolor="{CARD_BORDER}" style="background-color:{CARD_BORDER};height:1px;line-height:1px;font-size:1px;">&nbsp;</td></tr></table></td></tr>

    <tr><td class="bl-pad" style="padding:20px 40px 32px;">{support_html}{footer_note_html}<p class="bl-foot" style="margin:0;color:{MUTED_TEXT};font-family:{FONT_STACK};font-size:12px;line-height:1.6;">The BridgeLeads Team</p></td></tr>

  </table>

</td>
</tr>
</table>
</body>
</html>"""


def text_footer(*, footer_note: str | None = None) -> str:
    """The plain-text counterpart of the shared footer."""
    lines = [f"Questions? Reply to this email or contact {support_email()}."]
    if footer_note:
        lines.append(footer_note)
    lines.append("The BridgeLeads Team")
    return "\n\n".join(lines)
