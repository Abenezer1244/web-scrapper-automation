"""Commercial-email unsubscribe: GET shows a confirm page, POST records the opt-out.

Compliance audit 2026-11, Phase 4 (CAN-SPAM, 15 U.S.C. 7704(a)(3)). Every commercial
email carries this URL in its footer and in its List-Unsubscribe header, with a
token naming the ADDRESS (src/api/unsubscribe_tokens.py).

  GET  /email/unsubscribe?token=   Confirm page only. Never changes anything: mail
                                    security scanners pre-fetch links, and a GET
                                    that unsubscribed would opt people out unseen.
  POST /email/unsubscribe           The confirm page's button (token in the form
                                    body, not the URL) or a mail client's RFC 8058
                                    one-click POST (token in the URL, body
                                    "List-Unsubscribe=One-Click"). Adds the address
                                    to marketing_email_suppressions; repeating it is
                                    a no-op.

No session and no CSRF token: neither a mail client nor a link can carry one, and
the signed token is the whole capability (it can only add its own address). Every
outcome is a plain HTML page, and the pages never say whether an account exists.
Hidden from OpenAPI: browsers and mail clients call this, not the frontend.
"""
import html
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import audit_log, rate_limit
from src.api.unsubscribe_tokens import verify_unsubscribe_token
from src.config import settings
from src.db.session import get_db
from src.utils.logger import setup_logger

router = APIRouter(prefix="/email", tags=["email"], include_in_schema=False)
_logger = setup_logger("api.email_prefs")

# The token is a bearer capability in a URL: keep it out of caches and search
# indexes, and out of every Referer. SecurityHeadersMiddleware overwrites the
# Referrer-Policy HEADER with strict-origin-when-cross-origin, which would still send
# the full URL on the confirm form's same-origin POST, so the page sets
# <meta name="referrer" content="no-referrer">, which the header cannot override.
# The middleware's CSP (default-src 'none') also drops the inline styles below, as on
# the download-error page: the pages stay plain but readable; form-action is unaffected.
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
}
# A one-click body is "List-Unsubscribe=One-Click"; the confirm form sends a token.
_MAX_BODY_BYTES = 4096


async def _read_capped(request: Request) -> bytes | None:
    """The request body, or None once it passes _MAX_BODY_BYTES.

    Reads the stream itself, so a chunked or HTTP/2 body with no (or a false)
    Content-Length is bounded too.
    """
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _limit(request: Request, email_hmac: str | None) -> None:
    """Throttle per ADDRESS for a valid link, per IP otherwise.

    Mail providers send RFC 8058 one-click POSTs from a few shared IPs, and the RFC
    expects a 2xx: a per-IP bucket would let one busy provider IP turn other
    people's opt-outs into 429s. A valid token is bucketed by the address it names
    (a provider sends once per message), so only forged or broken links share the
    per-IP bucket. The general zone fails open, so a Redis outage never blocks an
    opt-out.
    """
    if email_hmac is None:
        await rate_limit(request, zone="general")
    else:
        await rate_limit(request, zone="general", identifier=f"unsubscribe:{email_hmac}")


def _page(title: str, body_html: str, status_code: int = 200) -> HTMLResponse:
    home = html.escape(settings.FRONTEND_URL)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)} | BridgeLeads</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 15vh auto; padding: 0 1rem; color: #1a1a1a">
<h1 style="font-size: 1.25rem">{html.escape(title)}</h1>
{body_html}
<p style="margin-top: 2rem"><a href="{home}">Back to BridgeLeads</a></p>
</body></html>""",
        status_code=status_code,
        headers=_HEADERS,
    )


def _invalid_link() -> HTMLResponse:
    return _page(
        "This unsubscribe link isn't valid",
        "<p>The link may be incomplete or out of date. Use the unsubscribe link in "
        "a recent BridgeLeads email, or reply to that email and we will remove you.</p>",
        status_code=400,
    )


@router.get("/unsubscribe")
async def unsubscribe_confirm(request: Request, token: str = "") -> HTMLResponse:
    email_hmac = verify_unsubscribe_token(token)
    await _limit(request, email_hmac)
    if email_hmac is None:
        return _invalid_link()
    # The token goes in a hidden field so the POST URL carries no token.
    return _page(
        "Unsubscribe from BridgeLeads emails?",
        "<p>You will stop getting product tips, reminders and offers from "
        "BridgeLeads. Emails about your account, security and billing will still "
        "arrive.</p>"
        # rel=noreferrer: the browser sends no Referer (which would carry this
        # page's token-bearing URL) on the POST, whatever the response policy says.
        '<form method="post" action="/email/unsubscribe" rel="noreferrer">'
        f'<input type="hidden" name="token" value="{html.escape(token)}">'
        '<button type="submit" style="font: inherit; padding: 0.6rem 1.2rem; '
        'border-radius: 6px; border: 0; background: #0f766e; color: #fff; cursor: pointer">'
        "Unsubscribe</button></form>",
    )


@router.post("/unsubscribe")
async def unsubscribe(
    request: Request,
    token: str = "",
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    body = await _read_capped(request)
    if body is None:
        await _limit(request, None)
        return _invalid_link()
    if token:
        # RFC 8058 one-click: the token is in the URL and the body is
        # "List-Unsubscribe=One-Click", sent as either form encoding. The body
        # cannot change the outcome (the URL alone is a valid opt-out), so it is
        # not parsed: no multipart parser ever sees untrusted input here.
        one_click, candidate = True, token
    else:
        # The confirm page's own form: urlencoded, token in the body.
        one_click = False
        try:
            fields = parse_qs(body.decode("utf-8", "replace"), max_num_fields=4)
        except ValueError:
            fields = {}
        candidate = (fields.get("token") or [""])[0]
    email_hmac = verify_unsubscribe_token(candidate)
    await _limit(request, email_hmac)
    if email_hmac is None:
        return _invalid_link()
    result = await db.execute(
        text(
            "INSERT INTO marketing_email_suppressions (email_hmac, source) "
            "VALUES (:h, :s) ON CONFLICT (email_hmac) DO NOTHING"
        ),
        {"h": email_hmac, "s": "one_click" if one_click else "link"},
    )
    await db.commit()
    if result.rowcount:
        # No address and no user id: the suppression row is the record.
        audit_log(request, "marketing_email_opted_out", None,
                  detail="one_click" if one_click else "link")
    return _page(
        "You're unsubscribed",
        "<p>You won't get product tips, reminders or offers from BridgeLeads at "
        "this address. Emails about your account, security and billing will still "
        "arrive.</p>",
    )
