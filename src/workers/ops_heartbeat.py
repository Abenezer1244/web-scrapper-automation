"""P0-02: outbound liveness heartbeat for beat + Redis + the worker.

Ops alerts (ops_alerts.py) only fire from a process that is running. A dead beat,
a dead worker or an unreachable broker sends nothing at all, so nobody hears about
it. This closes that gap from the outside: beat schedules ``ops_heartbeat`` on a
short interval, a worker consuming the ``celery`` queue runs it, and it pings an
external heartbeat monitor (Better Stack). When the pings stop, the monitor pages
the owner. One ping therefore proves beat scheduled, Redis delivered and a worker
ran a task. It does NOT prove the scrape/enrichment queue consumers or the
database (the db latency canary covers that).

Rules (Codex consult, P0-02):
- The URL is the credential (anyone holding it can fake liveness): it is never
  logged, failures log the exception class and status only, redirects are refused
  and only a 2xx counts.
- https only. Plain http is accepted for a loopback host alone, which never leaves
  the machine (local runs and the tests' real http.server).
- Never raises: a failed ping is simply a missing ping, which is the signal.
- Empty setting = off (dev/CI default).
"""
import urllib.request
from urllib.parse import urlsplit

from src.config import settings
from src.utils.logger import setup_logger
from src.workers.ops_alerts import _NoRedirect

_logger = setup_logger("worker.ops_heartbeat")

# Connect and read timeout, in seconds. Well under the beat interval and the task's
# queue expiry, so a hung monitor endpoint never stacks pings.
_HEARTBEAT_TIMEOUT_SECONDS = 10.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        # A malformed IPv6 host or an NFKC-invalid netloc raises, and the message
        # quotes the netloc. Refuse here so it can never escape into a task log.
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and host in _LOOPBACK_HOSTS


def ping_heartbeat() -> bool:
    """One GET to ``settings.OPS_HEARTBEAT_URL``. True when it answered 2xx.

    Stdlib ``urllib.request`` on purpose, as in ops_alerts._send_webhook: it logs
    nothing, while httpx and urllib3 log the request URL, and the token is the URL.
    """
    url = settings.OPS_HEARTBEAT_URL
    if not url:
        return False
    if not _allowed(url):
        _logger.error("ops heartbeat not sent: OPS_HEARTBEAT_URL must be an https URL")
        return False
    try:
        req = urllib.request.Request(url, method="GET")  # noqa: S310 — scheme checked by _allowed above
        # A 3xx with no redirect handler raises HTTPError, like a 4xx/5xx does.
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=_HEARTBEAT_TIMEOUT_SECONDS) as resp:
            if 200 <= resp.status < 300:
                return True
            _logger.warning("ops heartbeat not accepted: status=%s", resp.status)
            return False
    except Exception as exc:  # noqa: BLE001 — a failed ping is the signal, never an error upstream
        # Class + status only, never str(exc) or exc_info: HTTPError carries the URL
        # (exc.url), so a repr or a traceback is one refactor away from logging it.
        _logger.warning(
            "ops heartbeat failed: %s status=%s", type(exc).__name__, getattr(exc, "code", None)
        )
        return False
