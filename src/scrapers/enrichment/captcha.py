"""reCAPTCHA solver using 2Captcha service.

Solves reCAPTCHA v2 challenges and returns tokens for use in API calls.
Tokens are cached and reused until expired (~2 minutes).
"""

import asyncio
import time

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.captcha")

# Cache: {(sitekey, site_url, enterprise): (token, expiry_timestamp)}. Keyed on the
# full token CLASS, not the bare sitekey, so a v2 token can never be handed to an
# enterprise caller (or a staging URL's token to prod) if a key is ever shared (Codex).
_CacheKey = tuple[str, str, bool]
_token_cache: dict[_CacheKey, tuple[str, float]] = {}
_TOKEN_TTL = 100  # seconds (tokens valid ~2 min, use 100s to be safe)


async def solve_recaptcha(site_url: str, sitekey: str, *, enterprise: bool = False) -> str | None:
    """Solve a reCAPTCHA challenge and return the token.

    Uses 2Captcha API. Returns cached token if still valid.

    Args:
        site_url: The URL of the page with the reCAPTCHA.
        sitekey: The reCAPTCHA site key.
        enterprise: Solve as reCAPTCHA Enterprise (2Captcha ``enterprise=1``) — for
            score/invisible Enterprise integrations such as Pierce ATIP. Default is
            the classic v2 checkbox flow the King recorder uses.

    Returns:
        Solved reCAPTCHA token string, or None if solving failed.
    """
    if not settings.CAPTCHA_ENABLED or not settings.CAPTCHA_API_KEY:
        _logger.warning("CAPTCHA solving disabled (CAPTCHA_ENABLED=false or no API key)")
        return None

    key: _CacheKey = (sitekey, site_url, enterprise)
    # Check cache
    cached = _token_cache.get(key)
    if cached:
        token, expiry = cached
        if time.time() < expiry:
            _logger.info("Using cached reCAPTCHA token (%.0fs remaining)", expiry - time.time())
            return token

    # Solve via 2Captcha (with retry on UNSOLVABLE)
    _logger.info("Solving reCAPTCHA%s for %s (this takes ~15-30s)...",
                 " Enterprise" if enterprise else "", site_url)
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(settings.CAPTCHA_API_KEY)

    for attempt in range(1, 4):
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: solver.recaptcha(
                    sitekey=sitekey, url=site_url, enterprise=1 if enterprise else 0,
                ),
            )

            token = result.get("code", "")
            if not token:
                _logger.error("2Captcha returned empty token (status=%s)", result.get("status", "unknown"))
                continue

            _token_cache[key] = (token, time.time() + _TOKEN_TTL)
            _logger.info("reCAPTCHA solved (attempt %d), cached for %ds", attempt, _TOKEN_TTL)
            return token

        except Exception as exc:
            _logger.warning("2Captcha attempt %d failed: %s", attempt, exc)
            if attempt < 3:
                await asyncio.sleep(2)

    _logger.error("2Captcha failed after 3 attempts")
    return None


def invalidate_token(sitekey: str, site_url: str | None = None, *, enterprise: bool | None = None) -> None:
    """Remove cached token(s) for ``sitekey`` (e.g. when the API rejects one).

    With only ``sitekey`` (the King caller) every cached class for that key is
    dropped; ``site_url`` / ``enterprise`` narrow it to one token class.
    """
    for k in list(_token_cache):
        if k[0] != sitekey:
            continue
        if site_url is not None and k[1] != site_url:
            continue
        if enterprise is not None and k[2] != enterprise:
            continue
        _token_cache.pop(k, None)
