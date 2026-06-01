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
from urllib.parse import urljoin, urlparse

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


_REDIRECT_CODES = (301, 302, 303, 307, 308)


def safe_get_following(
    url: str,
    *,
    require_allowlisted: bool = False,
    require_https: bool = False,
    headers: dict | None = None,
    timeout: int = 10,
    max_redirects: int = 5,
) -> requests.Response:
    """Like ``safe_get`` but follows redirects, re-validating EVERY hop.

    For trusted-but-redirecting endpoints (e.g. a CDN that 302s to the actual
    object). `requests`' own redirect-following would jump to a `Location`
    without re-checking it — letting a validated public URL bounce to an
    internal/metadata host. Here each hop (initial URL AND every `Location`)
    is validated with `resolve=True`, so a redirect to a blocked target is
    refused. Caps the hop count. Raises `ValueError` on a blocked hop or too
    many redirects.
    """
    current = url
    for _ in range(max_redirects + 1):
        # require_https blocks a scheme downgrade (e.g. an HTTPS URL that 302s
        # to plaintext http://) which would leak a signed URL and allow MITM.
        if require_https and urlparse(current).scheme != "https":
            raise ValueError("HTTPS required for this fetch")
        validate_scraping_target(current, require_allowlisted=require_allowlisted, resolve=True)
        resp = _SESSION.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        if resp.status_code in _REDIRECT_CODES:
            location = resp.headers.get("Location")
            if not location:
                return resp
            current = urljoin(current, location)  # resolve relative Location
            continue
        return resp
    raise ValueError("Too many redirects")
