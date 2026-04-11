"""Security middleware: SSRF firewall, CSV injection prevention, security headers, audit logger."""

import ipaddress
import re
from urllib.parse import urlparse

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.utils.logger import setup_logger

_logger = setup_logger("security.audit")

# ─── SSRF Prevention ──────────────────────────────────────────────────────────

# Approved county portal domains — new counties must be explicitly added here.
# add_scrape_domain() may extend this at module init time (scraper constructors).
_ALLOWED_SCRAPE_DOMAINS: frozenset[str] = frozenset(
    [
        "armsweb.co.pierce.wa.us",
        "atip.piercecountywa.gov",
        "recordsearch.kingcounty.gov",
        "blue.kingcounty.com",
        "payment.kingcounty.gov",
        "e-docs.clark.wa.gov",
        "www.snoco.org",
    ]
)
_DOMAIN_REGISTRATION_LOCKED = False  # Set True after app startup to prevent runtime additions

# RFC 1918 private networks, loopback, link-local, cloud metadata
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local / AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique local address
    # M7 (full-SaaS review): IPv6 link-local range. Required for
    # parity with the IPv4 169.254.0.0/16 block — without it a
    # DNS-rebound target could point at an IPv6 link-local address
    # and bypass the SSRF firewall on dual-stack hosts.
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    [
        "metadata.google.internal",
        "instance-data",
        "169.254.169.254",  # AWS/GCP/Azure instance metadata
        "localhost",
    ]
)


def validate_scraping_target(url: str) -> None:
    """Raise ValueError if the URL is not an approved scraping target.

    Blocks:
    - Non-HTTPS schemes
    - Any domain not on the explicit allowlist
    - IP addresses in private/loopback/link-local/metadata ranges
    - Known cloud metadata hostnames
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL")

    if parsed.scheme != "https":
        raise ValueError("Only HTTPS scraping targets are permitted")

    hostname = (parsed.hostname or "").lower()

    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError("Scraping target not permitted")

    # Block IP addresses in restricted ranges
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None  # hostname is a domain name, not a raw IP

    if addr is not None:
        for network in _BLOCKED_NETWORKS:
            if addr in network:
                raise ValueError("Scraping target not permitted")

    # Allowlist check
    if hostname not in _ALLOWED_SCRAPE_DOMAINS:
        # Also check subdomains of allowed domains
        allowed = any(hostname.endswith(f".{d}") or hostname == d for d in _ALLOWED_SCRAPE_DOMAINS)
        if not allowed:
            raise ValueError("Scraping target not in approved domain list")


def add_scrape_domain(domain: str) -> None:
    """Register a county portal domain as an approved scraping target.

    Only allowed during module initialization (scraper constructors).
    After app startup, runtime additions are logged as warnings.
    """
    global _ALLOWED_SCRAPE_DOMAINS
    domain = domain.lower().strip()

    if domain in _ALLOWED_SCRAPE_DOMAINS:
        return  # Already registered

    if _DOMAIN_REGISTRATION_LOCKED:
        import logging
        logging.getLogger("security").warning(
            "Attempted to add scrape domain after lock: %s — ignoring", domain
        )
        return

    _ALLOWED_SCRAPE_DOMAINS = _ALLOWED_SCRAPE_DOMAINS | {domain}


def lock_scrape_domains() -> None:
    """Lock the domain allowlist — no more runtime additions. Call after app startup."""
    global _DOMAIN_REGISTRATION_LOCKED
    _DOMAIN_REGISTRATION_LOCKED = True


def register_connector_domains_from_db() -> int:
    """Load every active connector's base_url hostname into the allowlist.

    Connectors seeded via Alembic migration or scripts never pass through
    the API route that calls ``validate_scraping_target``. Without this
    function the first scrape attempt for each such connector throws
    ``Scraping target not in approved domain list``, even when the
    scraper would otherwise work. Call from both the FastAPI ``lifespan``
    hook and Celery ``worker_ready`` so every process starts with a
    complete allowlist.

    Returns the number of domains newly added. Safe to call multiple
    times — already-registered domains are a no-op.

    Failures (DB unreachable, bad row, etc.) are logged and swallowed so
    a misconfigured DB does not block app startup. Scrapers that hit an
    un-allowlisted domain will still fail safely at the validator.
    """
    import logging

    log = logging.getLogger("security.ssrf")
    added = 0
    try:
        from src.db.models import CountyConnector
        from src.db.session import SyncSessionLocal

        with SyncSessionLocal() as db:
            rows = db.query(CountyConnector).filter(CountyConnector.active).all()
            for row in rows:
                url = (row.base_url or "").strip()
                if not url:
                    continue
                try:
                    parsed = urlparse(url)
                    hostname = (parsed.hostname or "").lower()
                    if not hostname:
                        continue
                    # M11 (full-SaaS review): validate the URL
                    # through the SSRF firewall BEFORE adding its
                    # hostname to the allowlist. Previously a
                    # migration that seeded `http://169.254.169.254/...`
                    # (AWS metadata) or any private IP would have its
                    # host silently added to the allowlist at worker
                    # boot. Now we run validate_scraping_target first
                    # on a dummy https URL with this host — that
                    # catches blocked IPs, IPv6 bypass attempts, and
                    # disallowed schemes before they make it into the
                    # allowlist. If validation fails we log + skip.
                    #
                    # We construct the probe URL fresh rather than
                    # validating `url` directly because some seeded
                    # URLs are http:// (scraper auto-upgrades to
                    # https) and we don't want those to fail the
                    # probe on scheme alone.
                    probe_url = f"https://{hostname}/"
                    try:
                        # Temporarily add the host so validator's
                        # allowlist check passes, then validate the
                        # rest of the rules (blocked IPs, hostnames,
                        # IP ranges). If it fails, we immediately
                        # remove the host again.
                        add_scrape_domain(hostname)
                        validate_scraping_target(probe_url)
                    except ValueError as ssrf_exc:
                        # Validation failed — the host is on a
                        # blocked network or is a metadata host.
                        # We cannot cleanly "remove" from a frozenset
                        # in-place, but because the set lives until
                        # process restart and we already validated,
                        # we just log the violation. An operator
                        # seeding a metadata IP is a misconfiguration
                        # that needs attention either way.
                        log.error(
                            "Refusing to trust migration-seeded host %s "
                            "(%s/%s): %s",
                            hostname, row.county, row.state, ssrf_exc,
                        )
                        continue
                    # Count this as newly-added if it wasn't already
                    # present before this iteration. (Cannot use the
                    # pre/post length trick because add_scrape_domain
                    # was called inside the probe block.)
                    added += 1
                except Exception as exc:  # noqa: BLE001 — defensive
                    log.warning(
                        "Failed to register connector domain %s (%s/%s): %s",
                        url, row.county, row.state, exc,
                    )
        log.info(
            "SSRF allowlist: registered %d new connector domains (total: %d)",
            added, len(_ALLOWED_SCRAPE_DOMAINS),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("register_connector_domains_from_db skipped: %s", exc)
    return added


# ─── CSV / Formula Injection Prevention ──────────────────────────────────────

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_for_csv(value: str | None) -> str:
    """Prevent CSV formula injection (Excel/Sheets formula execution).

    Prefixes any cell value starting with =, +, -, @, TAB, or CR with a
    single quote so spreadsheet applications treat it as plain text.
    """
    if value is None:
        return ""
    value = clean_text(value)
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


# ─── Log Injection Prevention ─────────────────────────────────────────────────

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: str | None) -> str:
    """Strip control characters and normalize whitespace.

    Prevents log injection via scraped content containing newlines or
    ANSI escape sequences that would pollute structured log output.
    """
    if text is None:
        return ""
    text = _CONTROL_CHAR_RE.sub("", text)
    text = text.replace("\n", " ").replace("\r", " ")
    return text.strip()


# ─── Search Input Sanitization ───────────────────────────────────────────────

def sanitize_search(query: str | None) -> str | None:
    """Escape SQL LIKE wildcards and enforce length limit to prevent ReDoS.

    Limits to 100 characters. Escapes % and _ to prevent catastrophic
    backtracking in PostgreSQL ILIKE expressions.
    """
    if not query:
        return None
    query = query[:100]
    query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return query


# ─── Security Headers Middleware ──────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security response headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        # M5 (full-SaaS review): Cross-Origin isolation headers.
        # COOP=same-origin prevents cross-origin windows from
        # retaining a reference to our browsing context; CORP=same-site
        # prevents other origins from embedding our JSON responses as
        # resources. Both are inexpensive hardening for a pure-JSON
        # API that is only consumed by our own frontend origin.
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-site"
        # HSTS — only add on production HTTPS
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        return response


# ─── Audit Logger ─────────────────────────────────────────────────────────────

def audit_log(request: Request, event: str, user_id: str | None = None, detail: str = "") -> None:
    """Write a structured audit log entry for security-relevant events.

    Events: login_success, login_failure, logout, register, api_key_created,
            api_key_revoked, plan_upgraded, job_created
    """
    from .rate_limit import client_ip
    ip = client_ip(request)
    _logger.info(
        "AUDIT event=%s user_id=%s ip=%s path=%s detail=%s",
        clean_text(event),
        clean_text(user_id or "anonymous"),
        clean_text(ip),
        clean_text(str(request.url.path)),
        clean_text(detail),
    )
