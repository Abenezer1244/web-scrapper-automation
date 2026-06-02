"""Security middleware: SSRF firewall, CSV injection prevention, security headers, audit logger."""

import ipaddress
import re
import socket
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
# Hosts that legitimately serve only over HTTP (their HTTPS endpoint
# 404s or cert-fails). Membership here is an explicit per-host opt-in
# — being on _ALLOWED_SCRAPE_DOMAINS does NOT grant HTTP access.
# Populated at runtime by add_http_allowed_host() — scraper modules
# call it for known HTTP-only county portals (e.g. Grays Harbor
# EagleWeb), and POST /scrapers/connectors calls it when an admin
# seeds a connector with an http:// base_url.
_HTTP_ALLOWED_DOMAINS: frozenset[str] = frozenset()
_DOMAIN_REGISTRATION_LOCKED = False  # Set True after app startup to prevent runtime additions


def _host_matches_set(hostname: str, domain_set: frozenset[str]) -> bool:
    """True if hostname is in domain_set, or is a subdomain of any entry.

    SECURITY: allowlist entries MUST be specific FQDNs (e.g.
    ``recordsearch.kingcounty.gov``), never a registrable parent like
    ``kingcounty.gov`` — the ``.endswith`` subdomain match would then admit
    any sibling host (``evil.kingcounty.gov``). All current entries are full
    FQDNs; keep it that way when onboarding connectors.
    """
    if hostname in domain_set:
        return True
    return any(hostname.endswith(f".{d}") for d in domain_set)

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
    # 2026-06-01 security review (Codex cross-check): additional egress
    # ranges, relevant now that resolved IPs are checked (DNS rebinding).
    ipaddress.ip_network("0.0.0.0/8"),          # "this host" / unspecified
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT (RFC 6598)
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1
    ipaddress.ip_network("198.18.0.0/15"),      # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),        # multicast
    ipaddress.ip_network("240.0.0.0/4"),        # reserved (incl. 255.255.255.255)
    ipaddress.ip_network("::/128"),             # IPv6 unspecified
    ipaddress.ip_network("2001:db8::/32"),      # IPv6 documentation
    ipaddress.ip_network("ff00::/8"),           # IPv6 multicast
]

_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    [
        "metadata.google.internal",
        "instance-data",
        "169.254.169.254",  # AWS/GCP/Azure instance metadata
        "localhost",
        # S5: additional loopback aliases. These resolve to 127.0.0.1 / ::1
        # on common systems but are textual hostnames the literal-IP and
        # resolve checks may miss on a host with /etc/hosts overrides.
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "0",  # shorthand many resolvers map to 0.0.0.0 / 127.0.0.1
    ]
)


def _ip_is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if addr (or the IPv4 it maps to) falls in any blocked network.

    IPv4-mapped IPv6 (e.g. ``::ffff:169.254.169.254``) is normalized back
    to its IPv4 form so it can't dodge the IPv4 ranges.
    """
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return any(addr in network for network in _BLOCKED_NETWORKS)


def _idna_encodable(host: str) -> bool:
    """True if ``host`` can be IDNA-encoded to ASCII.

    S5: a host that cannot be IDNA-encoded (crafted unicode, empty label,
    over-long label) must be REJECTED by ``validate_scraping_target`` —
    not silently waved through. ASCII-only hosts and raw IP literals (no
    non-ASCII to encode) trivially pass. We keep ``_normalize_hostname``
    lossless/best-effort and centralize the fail-closed decision here so
    callers that only need a normalized string are unaffected.
    """
    if not host:
        return False
    try:
        host.encode("idna")
    except (UnicodeError, ValueError):
        # IP literals and some all-ASCII hosts raise on encode("idna")
        # (idna codec rejects e.g. labels with no alphanumerics); accept
        # them only when they are pure ASCII — non-ASCII that fails is unsafe.
        return host.isascii()
    return True


def _normalize_hostname(raw: str) -> str:
    """Lowercase, strip trailing dot and IPv6 zone id, IDNA-encode unicode."""
    host = (raw or "").strip().rstrip(".").lower()
    host = host.split("%", 1)[0]  # drop IPv6 zone id (e.g. fe80::1%eth0)
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass  # non-IDNA host; validate_scraping_target fails it closed (S5)
    return host


def _assert_resolved_ips_safe(hostname: str) -> None:
    """DNS-rebinding defense: reject if the host resolves to a blocked IP.

    The literal-IP check in ``validate_scraping_target`` is bypassable by
    a hostname that resolves to a private/loopback/metadata address. This
    resolves the host and rejects if ANY returned A/AAAA is blocked.
    Fails CLOSED — a resolution failure raises ``ValueError``.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        # gaierror (NXDOMAIN), timeout, and other resolver OSErrors all
        # fail closed as a clean ValueError so callers' blocked-return
        # path handles them (never escapes to crash a worker task).
        raise ValueError("Target host could not be resolved") from exc
    if not infos:
        raise ValueError("Target host did not resolve")
    for info in infos:
        ip_str = info[4][0].split("%", 1)[0]  # sockaddr[0], minus zone id
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise ValueError("Target resolved to an invalid address") from exc
        if _ip_is_blocked(addr):
            raise ValueError("Target resolves to a blocked address")


def validate_scraping_target(
    url: str, *, require_allowlisted: bool = True, resolve: bool = True
) -> None:
    """Raise ValueError if the URL is not an approved scraping target.

    Blocks:
    - Non-HTTP(S) schemes
    - IP addresses in private/loopback/link-local/metadata ranges
    - Known cloud metadata hostnames
    - With ``require_allowlisted=True`` (default): hosts not on the
      explicit allowlist, and HTTP scheme for any host not already
      on the allowlist.
    - With ``resolve=True`` (S5 — now the DEFAULT, fail-safe): hosts that
      RESOLVE to a blocked IP range (DNS-rebinding defense). Made the
      default so every caller is SSRF-safe by default; the only paths
      that opt OUT (``resolve=False``) are ones that must NOT perform DNS
      at validation time (e.g. allowlist registration at app/worker boot,
      where a transiently-unresolvable but legitimately-public host must
      not be silently dropped). Outbound-webhook validation also passes
      True explicitly.

    Pass ``require_allowlisted=False`` ONLY from admin onboarding paths
    that are about to call ``add_scrape_domain()`` for the validated
    host. The objective SSRF rules (scheme, blocked hostnames, blocked
    IP ranges) still apply — only the allowlist-membership check and
    the HTTP-must-be-allowlisted check are skipped.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid URL")

    if parsed.scheme not in ("https", "http"):
        raise ValueError("Only HTTP/HTTPS scraping targets are permitted")

    raw_hostname = (parsed.hostname or "").strip().rstrip(".").lower().split("%", 1)[0]
    hostname = _normalize_hostname(parsed.hostname or "")

    if not hostname:
        raise ValueError("Invalid URL: no hostname")

    # S5: fail closed on a host that cannot be IDNA-encoded. Previously the
    # bare `except (UnicodeError, ValueError): pass` in _normalize_hostname
    # let a crafted host fall through un-normalized and slip past the checks
    # below. A host we cannot canonicalize is not a host we can trust.
    if not _idna_encodable(raw_hostname):
        raise ValueError("Scraping target host could not be canonicalized")

    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError("Scraping target not permitted")

    # Block IP addresses in restricted ranges. Runs unconditionally so
    # admin onboarding cannot register a private/metadata IP host.
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None  # hostname is a domain name, not a raw IP

    if addr is not None and _ip_is_blocked(addr):
        raise ValueError("Scraping target not permitted")

    if require_allowlisted:
        # HTTP is only permitted for explicitly opted-in hosts. Being on
        # the general scrape allowlist (which is a tenant-isolation /
        # SSRF gate) is NOT enough — an HTTPS-capable portal accidentally
        # seeded with `http://` must still fail fast over plaintext. The
        # opt-in is by hostname OR parent domain so a portal at
        # `county.gov` that 302s to `www.county.gov` over HTTP works
        # without onboarding both forms.
        if parsed.scheme == "http" and not _host_matches_set(hostname, _HTTP_ALLOWED_DOMAINS):
            raise ValueError("HTTP is only permitted for explicitly opted-in scraping targets")

        # Allowlist check (subdomain-aware)
        if not _host_matches_set(hostname, _ALLOWED_SCRAPE_DOMAINS):
            raise ValueError("Scraping target not in approved domain list")

    # DNS-rebinding defense (opt-in). A literal-IP host was already
    # checked above, so only resolve real domain names.
    if resolve and addr is None:
        _assert_resolved_ips_safe(hostname)


def validate_outbound_webhook(url: str) -> None:
    """Validate a user-supplied outbound webhook URL (SSRF defense).

    Stricter than ``validate_scraping_target``: HTTPS only, no allowlist
    (users choose their own endpoint), and DNS resolution is REQUIRED so
    a host that resolves to a private/metadata IP is rejected. Call this
    immediately before the outbound POST so the check reflects current
    DNS. Raises ``ValueError`` if the target is not permitted.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Invalid webhook URL")
    if parsed.scheme != "https":
        raise ValueError("Webhook URL must use HTTPS")
    if len(url) > 2000:
        raise ValueError("Webhook URL too long")
    validate_scraping_target(url, require_allowlisted=False, resolve=True)


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


def add_http_allowed_host(domain: str) -> None:
    """Opt a host into HTTP scraping.

    Use ONLY for portals that genuinely have no working HTTPS endpoint
    (their HTTPS returns 404 or has a cert error). HTTPS is the default
    for every other host on the scrape allowlist, even ones that were
    seeded with an http:// base_url by mistake — that mistake should
    fail fast at the validator, not silently fall back to plaintext.
    Subdomains of opt-in hosts are also permitted (so a portal at
    `county.gov` that 302s to `www.county.gov` does not need both
    onboarded). Idempotent — calling twice with the same host is a no-op.
    """
    global _HTTP_ALLOWED_DOMAINS
    domain = domain.lower().strip()
    if not domain:
        return
    if domain in _HTTP_ALLOWED_DOMAINS:
        return
    _HTTP_ALLOWED_DOMAINS = _HTTP_ALLOWED_DOMAINS | {domain}


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
                    # M11 (full-SaaS review): validate the URL through
                    # the SSRF firewall BEFORE adding its hostname to
                    # the allowlist. Previously, a migration that
                    # seeded http://169.254.169.254/... (AWS metadata)
                    # or any private IP would have its host silently
                    # added at worker boot.
                    #
                    # require_allowlisted=False lets the validator
                    # check scheme + blocked-hostnames + blocked IP
                    # ranges WITHOUT requiring the host to already be
                    # on the allowlist (we're about to add it). Older
                    # versions of this code added first and validated
                    # second, which leaked failed hosts onto the
                    # frozen allowlist with no way to remove them.
                    try:
                        # S5: resolve=False here is intentional. This runs at
                        # app/worker boot to seed the allowlist from DB-config
                        # connectors; a legitimately-public host that is
                        # transiently unresolvable at boot must not be dropped.
                        # Scheme + blocked-hostname + blocked literal-IP checks
                        # still apply; the resolve-time DNS-rebinding check runs
                        # later on every actual outbound fetch (safe_http).
                        validate_scraping_target(
                            url, require_allowlisted=False, resolve=False
                        )
                    except ValueError as ssrf_exc:
                        log.error(
                            "Refusing to trust migration-seeded host %s "
                            "(%s/%s): %s",
                            hostname, row.county, row.state, ssrf_exc,
                        )
                        continue
                    # If this connector legitimately serves HTTP only, opt
                    # the host into the narrow HTTP allowlist. Validation
                    # above already confirmed the URL is safe SSRF-wise.
                    if parsed.scheme == "http":
                        add_http_allowed_host(hostname)
                    if hostname in _ALLOWED_SCRAPE_DOMAINS:
                        continue  # already registered, do not double-count
                    add_scrape_domain(hostname)
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

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
# Characters a spreadsheet strips from the front of a cell as quoting BEFORE
# it evaluates the contents. A value like `"=HYPERLINK(...)` (leading double
# quote) passes a naive first-char check yet still executes as a formula once
# Excel/Sheets unwrap the quote. (E1)
_WRAPPING_QUOTES = "'\"`"


def sanitize_for_csv(value: str | None) -> str:
    """Prevent CSV/spreadsheet formula injection (Excel/Sheets formula execution).

    Prefixes any cell value that resolves to a formula trigger (=, +, -, @,
    TAB, CR, LF) with a single quote so spreadsheet applications treat it as
    plain text.

    Three checks, because the spreadsheet sees a different string than Python:
    - RAW value (before clean_text) — catches a leading TAB/CR/LF that
      clean_text would strip away;
    - CLEANED value — catches leading whitespace like " \t=cmd";
    - DE-QUOTED probe — strips leading whitespace AND wrapping quote chars,
      catching `"=cmd` / `'=cmd` / `""=cmd` that the spreadsheet unwraps to a
      live formula. Checking only the first character leaves this bypass open.
    Non-str inputs (dates, numbers) are coerced.
    """
    if value is None:
        return ""
    raw = str(value)
    cleaned = clean_text(raw)
    probe = cleaned.lstrip().lstrip(_WRAPPING_QUOTES).lstrip()
    needs_quote = (
        raw.startswith(_FORMULA_PREFIXES)
        or cleaned.startswith(_FORMULA_PREFIXES)
        or probe.startswith(_FORMULA_PREFIXES)
    )
    return ("'" + cleaned) if needs_quote else cleaned


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
    # E3: replace TAB along with CR/LF. _CONTROL_CHAR_RE deliberately keeps
    # \t, but an embedded TAB acts as a column delimiter on TSV import /
    # copy-paste, splitting "123 Main\t=cmd" into a new cell that begins with
    # "=" and evaluates — a formula injection the leading-char check misses.
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
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
