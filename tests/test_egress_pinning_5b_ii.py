"""Audit #5 5b-ii: the PACS, AcclaimWeb-PACS and Tracerfy requests connect only to an
address the SSRF policy approved.

Each of these validated its URL with ``validate_scraping_target(resolve=True)`` and
then let a plain ``requests.Session`` resolve the host AGAIN to connect. A DNS answer
that changes between the two (rebinding) reached whatever the second answer named.
They now use ``pinned_session()``, the transport S3-08 put behind ``safe_http``.

The attack, reproduced end to end: a real listener on loopback stands in for the
internal service, and a rebinding DNS name answers PUBLIC while the real
``validate_scraping_target`` runs and LOOPBACK afterwards (a TTL-0 record). Nothing
else is replaced: the production validator, session and transport all run. These
URLs are HTTPS, so the listener counts TCP connections rather than HTTP requests:
the question is whether the worker ever opened a socket to the internal address.
"""
import asyncio
import socket
import threading

import pytest

from src.scrapers.base_scraper import ScrapedRecord
from src.scrapers.enrichment import pacs, skip_trace
from src.scrapers.templates import acclaimweb
from src.workers.skip_trace_dispatcher import classify_submit_failure


class _Listener:
    """Accepts and immediately closes TCP connections on loopback, counting them."""

    def __init__(self) -> None:
        self.accepted = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.fixture
def internal_service():
    listener = _Listener()
    yield listener
    listener.close()


_REBIND_HOST = "rebind.bridgeleads-test.example"
_PUBLIC_IP = "93.184.216.34"  # a public address; never dialled


@pytest.fixture
def rebinding_dns(monkeypatch):
    """``_REBIND_HOST`` resolves to a public address while the real validator runs,
    and to loopback at any other moment: exactly what a TTL-0 rebinding record
    does between the check and the connect. Every other name resolves normally."""
    real_getaddrinfo = socket.getaddrinfo
    phase = {"validating": False}

    def getaddrinfo(host, port, *args, **kwargs):
        if host != _REBIND_HOST:
            return real_getaddrinfo(host, port, *args, **kwargs)
        ip = _PUBLIC_IP if phase["validating"] else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    for module in (pacs, acclaimweb, skip_trace):
        real_validate = module.validate_scraping_target
        calls = {"n": 0}

        def validate(*args, _real=real_validate, _calls=calls, **kwargs):
            phase["validating"] = True
            try:
                _calls["n"] += 1
                return _real(*args, **kwargs)
            finally:
                phase["validating"] = False

        monkeypatch.setattr(module, "validate_scraping_target", validate)
        phase[module.__name__] = calls
    return phase


def _validated(dns, module) -> int:
    """How many times the REAL validator ran (and passed) for this module."""
    return dns[module.__name__]["n"]


def test_pacs_never_connects_to_an_internal_address(internal_service, rebinding_dns):
    url = f"https://{_REBIND_HOST}:{internal_service.port}/PropertyAccess/"
    assert pacs.lookup_pacs_by_name(url, "SMITH JOHN") is None
    assert _validated(rebinding_dns, pacs) == 1  # the real check ran and passed
    assert internal_service.accepted == 0


def test_acclaimweb_pacs_never_connects_to_an_internal_address(
    internal_service, rebinding_dns, monkeypatch,
):
    scraper = acclaimweb.AcclaimWebScraper(
        base_url="https://edocs.example.gov/AcclaimWeb", county="chelan", state="WA",
        record_types=["probate"],
    )
    monkeypatch.setattr(
        acclaimweb.AcclaimWebScraper, "_PACS_URLS",
        {"chelan": f"https://{_REBIND_HOST}:{internal_service.port}/PropertyAccess/"},
    )
    records = [ScrapedRecord(party_name="SMITH JOHN")]

    asyncio.run(scraper._lookup_pacs_addresses(records))

    assert _validated(rebinding_dns, acclaimweb) == 1
    assert internal_service.accepted == 0
    assert records[0].property_address is None


@pytest.fixture
def tracerfy_points_at(internal_service, monkeypatch):
    monkeypatch.setattr(
        skip_trace.settings, "TRACERFY_API_BASE_URL",
        f"https://{_REBIND_HOST}:{internal_service.port}",
    )
    return internal_service


def test_tracerfy_submit_never_sends_the_token_to_an_internal_address(
    tracerfy_points_at, rebinding_dns,
):
    rows = [{"address": "1 Main St", "city": "Seattle", "state": "WA", "zip": "98101",
             "first_name": "Jo", "last_name": "Doe"}]
    with pytest.raises(skip_trace.TracerfyError) as exc:
        skip_trace.submit_batch(rows, api_token="tok")

    assert _validated(rebinding_dns, skip_trace) == 1
    assert tracerfy_points_at.accepted == 0
    # Same verdict, and so the same dispatcher handling, as a refusal at
    # validation time: the rows are errored, never retried against a blocked host.
    assert str(exc.value).startswith("Refusing unsafe Tracerfy endpoint")
    assert classify_submit_failure(str(exc.value)) == "provider_error"


def test_tracerfy_queue_list_never_sends_the_token_to_an_internal_address(
    tracerfy_points_at, rebinding_dns,
):
    with pytest.raises(skip_trace.TracerfyError) as exc:
        skip_trace.fetch_queues(api_token="tok")

    assert _validated(rebinding_dns, skip_trace) == 1
    assert tracerfy_points_at.accepted == 0
    assert str(exc.value).startswith("Refusing unsafe Tracerfy endpoint")


def test_the_rebinding_name_really_reaches_the_listener_unpinned(internal_service, rebinding_dns):
    """Positive control: an UNPINNED connect to the rebinding name, after the real
    validator passed it, lands on the listener. So `accepted == 0` above means the
    pinned transport refused the second answer, not that the name or the listener
    was unreachable."""
    pacs.validate_scraping_target(
        f"https://{_REBIND_HOST}/", require_allowlisted=False, resolve=True,
    )
    with socket.create_connection((_REBIND_HOST, internal_service.port), timeout=2):
        pass
    for _ in range(20):
        if internal_service.accepted:
            break
        threading.Event().wait(0.05)
    assert internal_service.accepted == 1
