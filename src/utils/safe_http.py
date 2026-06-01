"""SSRF-safe outbound HTTP for server-side fetches.

Server-side `requests.get` calls that fetch a URL taken from scraped page
content (EagleWeb detail hrefs) or from DB config (county GIS endpoints) are
SSRF vectors. This module centralizes the safe path:

- validate through ``validate_scraping_target(resolve=True)`` so a host that
  resolves to a private/loopback/metadata IP is rejected (DNS-rebinding aware);
- ``allow_redirects=False`` so a 3xx can't bounce the request to an internal
  host after validation;
- optional ``same_origin_as`` pin so session cookies are only ever sent to the
  exact origin (scheme + host + port) that issued them — never a sibling or
  attacker host scraped out of the page.

Lives in utils (not on BridgeScraper) so plain enrichment modules like
``county_gis`` can use it without importing the scraper base class.
"""
from urllib.parse import urlparse

import requests

from src.api.middleware.security import _normalize_hostname, validate_scraping_target

_DEFAULT_PORTS = {"http": 80, "https": 443}

# trust_env=False: an ambient HTTP(S)_PROXY/NO_PROXY could otherwise move DNS
# resolution and the connection off-box (to the proxy), defeating the
# resolve=True SSRF check we just ran. We resolve + connect locally.
_SESSION = requests.Session()
_SESSION.trust_env = False


def _port_of(parsed) -> int | None:
    return parsed.port if parsed.port is not None else _DEFAULT_PORTS.get(parsed.scheme)


def same_origin(url: str, ref: str) -> bool:
    """True if ``url`` and ``ref`` share scheme + normalized host + port.

    Exact origin match — NOT subdomain-aware. ``portal.county.gov`` cookies
    must never reach ``evil.county.gov``.
    """
    a, b = urlparse(url), urlparse(ref)
    return (
        a.scheme == b.scheme
        and _normalize_hostname(a.hostname or "") == _normalize_hostname(b.hostname or "")
        and _port_of(a) == _port_of(b)
    )


def safe_get(
    url: str,
    *,
    same_origin_as: str | None = None,
    require_allowlisted: bool = False,
    params: dict | None = None,
    cookies: dict | None = None,
    headers: dict | None = None,
    timeout: int = 10,
) -> requests.Response:
    """SSRF-guarded ``requests.get``. Raises ``ValueError`` if not permitted.

    Args:
        url: absolute URL to fetch (resolve relative hrefs before calling).
        same_origin_as: if set, ``url`` must be the same origin or the call is
            refused — use this whenever ``cookies`` carry an authenticated
            session so they can't leak cross-origin.
        require_allowlisted: pass True to also require the host be on the
            scrape allowlist; default False (block private/metadata IPs and
            resolve, but allow any public host — for GIS/3rd-party endpoints).
    """
    validate_scraping_target(url, require_allowlisted=require_allowlisted, resolve=True)
    if same_origin_as is not None and not same_origin(url, same_origin_as):
        raise ValueError("Refusing to send request to a different origin")
    return _SESSION.get(
        url,
        params=params,
        cookies=cookies,
        headers=headers,
        timeout=timeout,
        allow_redirects=False,  # a 3xx must not bounce us to an internal host
    )
