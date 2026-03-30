"""Navigation action cache for AI scraper.

After a successful AI scrape, caches the navigation actions for that base_url.
On subsequent runs, tries cached actions first — only calls Claude if they fail.
This turns a ~$0.05/run Claude call into a ~$0.00/run cached replay.

Cache is stored in Redis with a 7-day TTL.
"""

import json

import redis as sync_redis

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ai.cache")

_CACHE_PREFIX = "ai_nav_cache:"
_CACHE_TTL = 7 * 24 * 3600  # 7 days


def _redis() -> sync_redis.Redis:
    kwargs = {}
    if settings.REDIS_URL.startswith("rediss://"):
        import ssl
        kwargs["ssl_cert_reqs"] = ssl.CERT_NONE  # Upstash uses custom certs not in system CA
    return sync_redis.from_url(settings.REDIS_URL, decode_responses=True, **kwargs)


def _cache_key(base_url: str, record_type: str) -> str:
    """Build a cache key from the base URL and record type."""
    return f"{_CACHE_PREFIX}{base_url}:{record_type}"


def get_cached_actions(base_url: str, record_type: str) -> list[dict] | None:
    """Retrieve cached navigation actions for a county portal.

    Args:
        base_url: The county portal URL.
        record_type: The record type (e.g. "probate").

    Returns:
        List of action dicts if cached, None if not found.
    """
    try:
        r = _redis()
        key = _cache_key(base_url, record_type)
        raw = r.get(key)
        if raw:
            actions = json.loads(raw)
            _logger.info("Cache HIT for %s (%s) — %d actions", base_url, record_type, len(actions))
            return actions
    except Exception as exc:
        _logger.warning("Cache read error: %s", exc)

    _logger.info("Cache MISS for %s (%s)", base_url, record_type)
    return None


def save_cached_actions(base_url: str, record_type: str, actions: list[dict]) -> None:
    """Save navigation actions to cache after a successful scrape.

    Args:
        base_url: The county portal URL.
        record_type: The record type.
        actions: The list of navigation actions from Claude.
    """
    if not actions:
        return

    try:
        r = _redis()
        key = _cache_key(base_url, record_type)
        r.setex(key, _CACHE_TTL, json.dumps(actions))
        _logger.info("Cached %d actions for %s (%s), TTL=%dd", len(actions), base_url, record_type, _CACHE_TTL // 86400)
    except Exception as exc:
        _logger.warning("Cache write error: %s", exc)


def invalidate_cache(base_url: str, record_type: str) -> None:
    """Remove cached actions (e.g. when a cached replay fails)."""
    try:
        r = _redis()
        key = _cache_key(base_url, record_type)
        r.delete(key)
        _logger.info("Cache invalidated for %s (%s)", base_url, record_type)
    except Exception as exc:
        _logger.warning("Cache invalidation error: %s", exc)
