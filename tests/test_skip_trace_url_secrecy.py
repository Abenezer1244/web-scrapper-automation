"""The signed CSV download URL must never reach a log, message, or traceback.

Tracerfy delivers results from a CDN link that carries a signed token. Anyone
holding that URL can download another tenant's skip-traced PII, so it is a
credential, not just an address.

download_tracerfy_csv is written to keep it out of error paths, but until now
only a COMMENT said so. A future edit swapping `type(exc).__name__` back to
`{exc}`, or dropping `from None`, would silently start writing customer tokens
into production logs and every existing test would still pass.

These tests make that guarantee executable. They assert on the ABSENCE of the
token in three places, because a leak reaches all three differently:
  * the raised exception's own message,
  * the __cause__/__context__ chain, which a traceback prints,
  * anything emitted to the logger.
"""

import logging

import pytest
import requests

from src.scrapers.enrichment import skip_trace
from src.scrapers.enrichment.skip_trace import TracerfyError, download_tracerfy_csv

# A realistic signed CDN link. TOKEN is the part that must never appear anywhere.
# The bare signature VALUE is asserted separately from the parameter name:
# a sentinel containing "X-Amz-Signature-" could pass while only the secret half
# leaked, which is the half that matters (Codex).
SIG = "deadbeefcafe1234567890abcdefSECRETSIG"
TOKEN = f"X-Amz-Signature-{SIG}"
SIGNED_URL = (
    "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/abc.csv"
    f"?X-Amz-Credential=AKIAEXAMPLE&{TOKEN}"
)


def _assert_no_token(exc: BaseException, caplog) -> None:
    """The token must not be in the message, the chain, the notes, or the logs.

    Asserts on the bare signature too, not just the full parameter, so a partial
    leak cannot slip through (Codex).
    """
    import traceback

    for needle, what in ((TOKEN, "token"), (SIG, "bare signature")):
        assert needle not in str(exc), f"{what} leaked into the exception message"

    # A formatted traceback is what actually reaches a log or an error tracker,
    # and it includes exception notes that str(exc) omits entirely.
    formatted = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    for needle, what in ((TOKEN, "token"), (SIG, "bare signature")):
        assert needle not in formatted, f"{what} leaked into the formatted traceback"

    # Walk the chain the way Python's traceback module does: __cause__ always,
    # __context__ only when it is NOT suppressed. `raise ... from None` sets
    # __suppress_context__, which is precisely how these paths keep the original
    # requests/ValueError (and its embedded URL) out of printed tracebacks.
    chain, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(str(cur))
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif not cur.__suppress_context__:
            cur = cur.__context__
        else:
            break
    joined = " | ".join(chain)
    assert TOKEN not in joined, f"signed token leaked via the exception chain: {joined[:200]}"

    # Format each record fully: getMessage() omits exc_info and stack_info,
    # which is exactly where a leaked URL would hide (Codex).
    fmt = logging.Formatter("%(message)s")
    logged = " | ".join(fmt.format(r) for r in caplog.records)
    for needle, what in ((TOKEN, "token"), (SIG, "bare signature")):
        assert needle not in logged, f"{what} leaked into the logs"


def test_transport_failure_does_not_leak_the_token(monkeypatch, caplog):
    """requests embeds the full URL in its exception string. Interpolating it
    would put the token in the message AND, via __cause__, in any traceback."""
    def _boom(url, **kw):
        raise requests.ConnectionError(f"Failed to establish a connection to {url}")

    monkeypatch.setattr(skip_trace, "safe_get_following", _boom)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TracerfyError) as ei:
            download_tracerfy_csv(SIGNED_URL)
    _assert_no_token(ei.value, caplog)


def test_ssrf_rejection_does_not_leak_the_token(monkeypatch, caplog):
    """The SSRF guard raises ValueError, and its message can legitimately quote
    the URL it refused. That path must be scrubbed like the transport one."""
    def _refuse(url, **kw):
        raise ValueError(f"Refusing to fetch {url}: host resolves to a private range")

    monkeypatch.setattr(skip_trace, "safe_get_following", _refuse)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TracerfyError) as ei:
            download_tracerfy_csv(SIGNED_URL)
    _assert_no_token(ei.value, caplog)


def test_non_200_does_not_leak_the_token(monkeypatch, caplog):
    class _Resp:
        status_code = 403
        text = "denied"

    monkeypatch.setattr(skip_trace, "safe_get_following", lambda url, **kw: _Resp())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TracerfyError) as ei:
            download_tracerfy_csv(SIGNED_URL)
    _assert_no_token(ei.value, caplog)


def test_the_happy_path_still_returns_the_csv(monkeypatch):
    """The scrubbing must not break the thing it protects."""
    class _Resp:
        status_code = 200
        text = "address,city,state\n1 MAIN ST,TACOMA,WA\n"

    monkeypatch.setattr(skip_trace, "safe_get_following", lambda url, **kw: _Resp())
    assert download_tracerfy_csv(SIGNED_URL).startswith("address,city,state")


def test_a_malformed_url_cannot_leak_via_a_secondary_exception(monkeypatch, caplog):
    """The leak Codex found in the FIX itself.

    The refusal branch parses the URL to log its host. urlsplit() rejects some
    malformed inputs (bad IPv6 brackets), and an exception escaping there would
    propagate with the original ValueError -- URL and all -- as its __context__.
    That is the very leak the branch exists to prevent, reintroduced by the
    logging added to it. _safe_host() must swallow it.
    """
    bad = f"https://[not-an-ipv6/tracerfy/abc.csv?{TOKEN}"

    def _refuse(url, **kw):
        raise ValueError(f"Refusing to fetch {url}: host resolves to a private range")

    monkeypatch.setattr(skip_trace, "safe_get_following", _refuse)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TracerfyError) as ei:
            download_tracerfy_csv(bad)
    _assert_no_token(ei.value, caplog)


def test_the_refusal_reason_survives_redaction(caplog, monkeypatch):
    """Redacting must not destroy the diagnosis. ValueError here covers DNS
    failure, a blocked address, an HTTPS downgrade, a malformed URL and redirect
    exhaustion alike, so collapsing it to 'ValueError' would leave an operator
    with nothing to act on (Codex)."""
    def _refuse(url, **kw):
        raise ValueError(f"Refusing to fetch {url}: host resolves to a private range")

    monkeypatch.setattr(skip_trace, "safe_get_following", _refuse)
    with pytest.raises(TracerfyError) as ei:
        download_tracerfy_csv(SIGNED_URL)
    assert "private range" in str(ei.value), "the refusal reason was destroyed"
    assert "<redacted" in str(ei.value), "the URL was not redacted"
