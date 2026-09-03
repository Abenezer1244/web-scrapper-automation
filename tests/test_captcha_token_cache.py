"""solve_recaptcha token cache is keyed by (sitekey, site_url, enterprise): a v2
token must never be handed to an Enterprise caller (Codex). No network — the
cache dict is seeded directly."""
import time

from src.scrapers.enrichment import captcha


def _seed(sitekey, url, enterprise, token):
    captcha._token_cache[(sitekey, url, enterprise)] = (token, time.time() + 60)


def test_invalidate_by_sitekey_only_drops_every_class():
    captcha._token_cache.clear()
    _seed("K", "https://a", False, "t1")
    _seed("K", "https://a", True, "t2")
    _seed("OTHER", "https://a", False, "t3")
    captcha.invalidate_token("K")
    assert set(captcha._token_cache) == {("OTHER", "https://a", False)}
    captcha._token_cache.clear()


def test_invalidate_narrowed_to_one_class():
    captcha._token_cache.clear()
    _seed("K", "https://a", False, "t1")
    _seed("K", "https://a", True, "t2")
    captcha.invalidate_token("K", "https://a", enterprise=True)
    assert set(captcha._token_cache) == {("K", "https://a", False)}
    captcha._token_cache.clear()
