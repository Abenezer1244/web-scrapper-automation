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
# indexes. Referer is already safe: SecurityHeadersMiddleware sets
# strict-origin-when-cross-origin on every response, and the only link here goes to
# another origin, so it carries the origin alone. That middleware's CSP
# (default-src 'none') also drops the inline styles below, as on the download-error
# page: the pages stay plain but readable, and form-action is unaffected.
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
}
_ONE_CLICK = ("List-Unsubscribe", "One-Click")


def _page(title: str, body_html: str, status_code: int = 200) -> HTMLResponse:
    home = html.escape(settings.FRONTEND_URL)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
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
    await rate_limit(request, zone="general")
    if verify_unsubscribe_token(token) is None:
        return _invalid_link()
    # The token goes in a hidden field so the POST URL carries no token.
    return _page(
        "Unsubscribe from BridgeLeads emails?",
        "<p>You will stop getting product tips, reminders and offers from "
        "BridgeLeads. Emails about your account, security and billing will still "
        "arrive.</p>"
        '<form method="post" action="/email/unsubscribe">'
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
    await rate_limit(request, zone="general")
    form = await request.form()
    one_click = form.get(_ONE_CLICK[0]) == _ONE_CLICK[1]
    # One-click: token in the URL (RFC 8058). Confirm page: token in the body.
    email_hmac = verify_unsubscribe_token(token or str(form.get("token") or ""))
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
