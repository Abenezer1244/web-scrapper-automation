"""Commercial-email unsubscribe tokens and links (purpose=unsubscribe).

Minted by the senders of commercial email (src/workers/onboarding_emails.py) and
verified by /email/unsubscribe (src/api/routes/email_prefs.py). Compliance audit
2026-11, Phase 4 (CAN-SPAM opt-out).

The token names an ADDRESS, never an account: its only claim of substance is the
address's email_hmac (the keyed blind index, never the address itself), which is the
key of marketing_email_suppressions. So an opt-out cannot follow a user to a new
address after an email change, and it still works after the account is deleted.

It is a capability, not a credential: holding it can only ADD that one address to
the suppression list, which is idempotent and harmless to repeat. Hence no jti and
no revocation. It lives 400 days because the law requires the opt-out to keep
working for at least 30 days after each send, and old emails get clicked late.

Kept dependency-light (no FastAPI / redis imports) so Celery workers can mint.
"""
import re
import time
from urllib.parse import quote, urlsplit

import jwt

from src.config import settings
from src.utils.crypto import blind_index

_ALGORITHM = "HS256"
_AUDIENCE = "bridgeleads-unsubscribe"
_ISSUER = "bridgeleads"
_PURPOSE = "unsubscribe"
_TTL_SECONDS = 400 * 24 * 3600
_HMAC = re.compile(r"^[0-9a-f]{64}$")


def mint_unsubscribe_token(email: str) -> str:
    """Return a signed unsubscribe token for this address."""
    now = int(time.time())
    return jwt.encode(
        {
            "purpose": _PURPOSE,
            "ehm": blind_index(email),
            "aud": _AUDIENCE,
            "iss": _ISSUER,
            "iat": now,
            "exp": now + _TTL_SECONDS,
        },
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )


def verify_unsubscribe_token(token: str) -> str | None:
    """The email_hmac a valid token names, or None for anything else.

    None covers expired, forged, wrong-audience and wrong-purpose tokens alike: the
    caller shows one generic page and never says which, or whether any account
    exists for the address.
    """
    if not token or len(token) > 2048:
        return None
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "aud", "iss", "purpose", "ehm"]},
        )
    except jwt.InvalidTokenError:
        return None
    ehm = claims.get("ehm")
    if claims.get("purpose") != _PURPOSE or not isinstance(ehm, str) or not _HMAC.match(ehm):
        return None
    return ehm


def public_api_base() -> str | None:
    """The public API origin for emailed links, or None if it is not usable.

    An unsubscribe link must work from any mail client, so it must be an absolute
    https URL (http only for a local host). Anything else, including an empty
    setting, is unusable, and a commercial email that cannot carry a working link
    is not sent.
    """
    base = (settings.API_BASE_URL or "").strip().rstrip("/")
    if not base or any(c in base for c in "\r\n <>\"'"):
        return None
    try:
        parts = urlsplit(base)
        parts.port  # noqa: B018 -- raises ValueError on a malformed port
        hostname = parts.hostname
    except ValueError:  # malformed authority, e.g. an unclosed IPv6 bracket
        return None
    local = hostname in ("localhost", "127.0.0.1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        return None
    # A bare origin: the route is mounted at the API root, so any path, query,
    # fragment or userinfo would build a link that does not reach it.
    if not hostname or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    return base


def unsubscribe_url(email: str) -> str | None:
    """The one-click unsubscribe URL for this address, or None if it cannot be built."""
    base = public_api_base()
    if base is None:
        return None
    return f"{base}/email/unsubscribe?token={quote(mint_unsubscribe_token(email), safe='')}"
