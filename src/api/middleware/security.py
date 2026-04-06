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
    ipaddress.ip_network("fc00::/7"),
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
        # HSTS — only add on production HTTPS
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        return response


# ─── Audit Logger ─────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def audit_log(request: Request, event: str, user_id: str | None = None, detail: str = "") -> None:
    """Write a structured audit log entry for security-relevant events.

    Events: login_success, login_failure, logout, register, api_key_created,
            api_key_revoked, plan_upgraded, job_created
    """
    ip = _client_ip(request)
    _logger.info(
        "AUDIT event=%s user_id=%s ip=%s path=%s detail=%s",
        clean_text(event),
        clean_text(user_id or "anonymous"),
        clean_text(ip),
        clean_text(str(request.url.path)),
        clean_text(detail),
    )
