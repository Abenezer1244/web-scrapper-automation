"""P0-10: warn before Tracerfy runs out of credits, not after.

Skip tracing for every tenant draws on ONE Tracerfy credit balance. The only signal
used to be the 402 a batch gets once the balance cannot cover it (the dispatcher's
out-of-credits page), by which time every tenant's lookups have already stalled:
on 2026-09-02 that lasted 7+ hours. Tracerfy has no low-balance webhook, so beat
asks once an hour (GET /v1/api/analytics/, its documented "Usage Analytics"
endpoint, whose ``balance`` is the account's current credit balance) and pages
the owner while the balance sits below TRACERFY_LOW_BALANCE_CREDITS.

Rules (Codex consult, P0-10):
- Off (no network call at all) unless the threshold is above 0, skip tracing is
  enabled and a token is configured.
- Same egress posture as skip_trace.fetch_queues: https only, the real SSRF
  validator with resolve=True before the token is sent, the pinned transport,
  no ambient proxy, no redirects. Only a 200 counts.
- ``balance`` must be a JSON int >= 0. Anything else is a broken monitor and
  pages as one: a silent schema change must never read as "fine" or as "empty".
- A rejected key (401/403: revoked, suspended, API disabled) pages. A timeout,
  DNS/TLS failure, 429 or 5xx is logged and retried next hour.
- Never logged or alerted: the token, response bodies, exception text.
- The low-balance page has its own cooldown bucket, separate from the 402 page,
  so a warning can never suppress the out-of-credits alert.
"""
import requests

from src.api.middleware.security import validate_scraping_target
from src.config import settings
from src.utils.logger import setup_logger
from src.utils.pinned_http import is_blocked_destination, pinned_session
from src.workers.ops_alerts import send_ops_alert

_logger = setup_logger("worker.tracerfy_balance")

_TIMEOUT_SECONDS = 30

# BalanceError categories. Each says what happened without quoting the response.
UNAUTHORIZED = "unauthorized"        # 401/403: the key was refused
MALFORMED = "malformed"              # not a 200, or no int balance in it
UNSAFE_ENDPOINT = "unsafe_endpoint"  # the configured endpoint failed the egress policy
TRANSIENT = "transient"              # timeout, connection, TLS, 429, 5xx: try next tick
NOT_CONFIGURED = "not_configured"


class BalanceError(Exception):
    """The balance could not be read. ``category`` is one of the constants above."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def fetch_balance(timeout: int = _TIMEOUT_SECONDS) -> int:
    """The current Tracerfy credit balance. Raises BalanceError, never anything else."""
    token = settings.TRACERFY_API_TOKEN
    if not token:
        raise BalanceError(NOT_CONFIGURED)
    url = f"{settings.TRACERFY_API_BASE_URL.rstrip('/')}/v1/api/analytics/"
    if not url.lower().startswith("https://"):
        raise BalanceError(UNSAFE_ENDPOINT)
    try:
        validate_scraping_target(url, require_allowlisted=False, resolve=True)
    except ValueError:
        raise BalanceError(UNSAFE_ENDPOINT) from None

    try:
        with pinned_session() as session:
            resp = session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
                allow_redirects=False,
            )
    except requests.RequestException as exc:
        if is_blocked_destination(exc):
            raise BalanceError(UNSAFE_ENDPOINT) from None
        raise BalanceError(TRANSIENT) from None

    status = resp.status_code
    if status in (401, 403):
        raise BalanceError(UNAUTHORIZED)
    if status == 429 or status >= 500:
        raise BalanceError(TRANSIENT)
    if status != 200:
        raise BalanceError(MALFORMED)
    try:
        data = resp.json()
    except ValueError:
        raise BalanceError(MALFORMED) from None
    balance = data.get("balance") if isinstance(data, dict) else None
    # type() and not isinstance(): True is an int, and must not read as 1 credit.
    if type(balance) is not int or balance < 0:
        raise BalanceError(MALFORMED)
    return balance


def check_tracerfy_balance() -> str:
    """One hourly check. Returns the outcome: off, ok, low or a BalanceError category."""
    threshold = settings.TRACERFY_LOW_BALANCE_CREDITS
    if threshold <= 0:
        _logger.info("tracerfy balance check off: TRACERFY_LOW_BALANCE_CREDITS is 0")
        return "off"
    if not settings.SKIP_TRACE_ENABLED:
        _logger.info("tracerfy balance check off: skip tracing is disabled")
        return "off"
    if not settings.TRACERFY_API_TOKEN:
        _logger.info("tracerfy balance check off: no Tracerfy token")
        return "off"

    try:
        balance = fetch_balance()
    except BalanceError as exc:
        if exc.category == UNAUTHORIZED:
            _logger.error("tracerfy balance check: the API key was refused (401/403)")
            send_ops_alert(
                "tracerfy_account", "account",
                "Tracerfy refused the API key",
                "The hourly balance check got 401 or 403 from Tracerfy: the key may be "
                "revoked, or the account suspended or its API access disabled. Skip "
                "tracing cannot submit until this is fixed. Check the account at "
                "tracerfy.com and TRACERFY_API_TOKEN on the worker.",
            )
        elif exc.category in (MALFORMED, UNSAFE_ENDPOINT, NOT_CONFIGURED):
            _logger.error("tracerfy balance check broken: %s", exc.category)
            send_ops_alert(
                "tracerfy_balance_monitor", "account",
                "Tracerfy balance check is not working",
                f"The hourly balance check could not read a credit balance ({exc.category}). "
                "Low-balance warnings are off until it is fixed; the out-of-credits page "
                "still works. See src/workers/tracerfy_balance.py.",
            )
        else:
            _logger.warning("tracerfy balance check failed (%s); retrying next hour", exc.category)
        return exc.category

    if balance < threshold:
        _logger.warning("tracerfy balance LOW: %d credits, threshold %d", balance, threshold)
        send_ops_alert(
            "tracerfy_low_balance", "account",
            f"Tracerfy credits low: {balance:,} left",
            f"The Tracerfy balance is {balance:,} credits, under the {threshold:,} credit "
            "warning threshold (TRACERFY_LOW_BALANCE_CREDITS). Skip tracing stops for "
            "every customer when it reaches zero. Add credits at tracerfy.com.",
        )
        return "low"
    _logger.info("tracerfy balance ok: %d credits, threshold %d", balance, threshold)
    return "ok"
