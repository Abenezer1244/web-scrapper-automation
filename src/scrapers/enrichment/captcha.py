"""reCAPTCHA solver using 2Captcha service.

Solves reCAPTCHA v2 challenges and returns tokens for use in API calls.
Tokens are cached and reused until expired (~2 minutes).
"""

import asyncio
import time

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.captcha")

# Cache: {sitekey: (token, expiry_timestamp)}
_token_cache: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 100  # seconds (tokens valid ~2 min, use 100s to be safe)


async def solve_recaptcha(site_url: str, sitekey: str) -> str | None:
    """Solve a reCAPTCHA v2 challenge and return the token.

    Uses 2Captcha API. Returns cached token if still valid.

    Args:
        site_url: The URL of the page with the reCAPTCHA.
        sitekey: The reCAPTCHA site key.

    Returns:
        Solved reCAPTCHA token string, or None if solving failed.
    """
    if not settings.CAPTCHA_ENABLED or not settings.CAPTCHA_API_KEY:
        _logger.warning("CAPTCHA solving disabled (CAPTCHA_ENABLED=false or no API key)")
        return None

    # Check cache
    cached = _token_cache.get(sitekey)
    if cached:
        token, expiry = cached
        if time.time() < expiry:
            _logger.info("Using cached reCAPTCHA token (%.0fs remaining)", expiry - time.time())
            return token

    # Solve via 2Captcha (with retry on UNSOLVABLE)
    _logger.info("Solving reCAPTCHA for %s (this takes ~15-30s)...", site_url)
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(settings.CAPTCHA_API_KEY)

    for attempt in range(1, 4):
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: solver.recaptcha(sitekey=sitekey, url=site_url),
            )

            token = result.get("code", "")
            if not token:
                _logger.error("2Captcha returned empty token (status=%s)", result.get("status", "unknown"))
                continue

            _token_cache[sitekey] = (token, time.time() + _TOKEN_TTL)
            _logger.info("reCAPTCHA solved (attempt %d), cached for %ds", attempt, _TOKEN_TTL)
            return token

        except Exception as exc:
            _logger.warning("2Captcha attempt %d failed: %s", attempt, exc)
            if attempt < 3:
                await asyncio.sleep(2)

    _logger.error("2Captcha failed after 3 attempts")
    return None


def invalidate_token(sitekey: str) -> None:
    """Remove a cached token (e.g. when the API rejects it)."""
    _token_cache.pop(sitekey, None)
