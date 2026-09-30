"""Audit #5 5b-ii: the PACS, AcclaimWeb-PACS and Tracerfy requests connect only to an
address the SSRF policy approved.

Each of these validated its URL with ``validate_scraping_target(resolve=True)`` and
then let a plain ``requests.Session`` resolve the host AGAIN to connect. A DNS answer
that changes between the two (rebinding) reached whatever the second answer named.
They now use ``pinned_session()``, the transport S3-08 put behind ``safe_http``.

Same method as tests/test_egress_pinning_s3_08.py: a real listener on loopback
stands in for the internal service a rebinding attacker aims at, and only the
pre-check is replaced, with "public" (the attacker's first answer). These URLs are
HTTPS, so the listener counts TCP connections rather than HTTP requests: the
question is whether the worker ever opened a socket to the internal address.
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


@pytest.fixture
def precheck_says_public(monkeypatch):
    """The rebinding attacker's first DNS answer: every pre-check passes."""
    for module in (pacs, acclaimweb, skip_trace):
        monkeypatch.setattr(module, "validate_scraping_target", lambda *a, **k: None)


def test_pacs_never_connects_to_an_internal_address(internal_service, precheck_says_public):
    url = f"https://127.0.0.1:{internal_service.port}/PropertyAccess/"
    assert pacs.lookup_pacs_by_name(url, "SMITH JOHN") is None
    assert internal_service.accepted == 0


def test_acclaimweb_pacs_never_connects_to_an_internal_address(
    internal_service, precheck_says_public, monkeypatch,
):
    scraper = acclaimweb.AcclaimWebScraper(
        base_url="https://edocs.example.gov/AcclaimWeb", county="chelan", state="WA",
        record_types=["probate"],
    )
    monkeypatch.setattr(
        acclaimweb.AcclaimWebScraper, "_PACS_URLS",
        {"chelan": f"https://127.0.0.1:{internal_service.port}/PropertyAccess/"},
    )
    records = [ScrapedRecord(party_name="SMITH JOHN")]

    asyncio.run(scraper._lookup_pacs_addresses(records))

    assert internal_service.accepted == 0
    assert records[0].property_address is None


@pytest.fixture
def tracerfy_points_at(internal_service, monkeypatch):
    monkeypatch.setattr(
        skip_trace.settings, "TRACERFY_API_BASE_URL",
        f"https://127.0.0.1:{internal_service.port}",
    )
    return internal_service


def test_tracerfy_submit_never_sends_the_token_to_an_internal_address(
    tracerfy_points_at, precheck_says_public,
):
    rows = [{"address": "1 Main St", "city": "Seattle", "state": "WA", "zip": "98101",
             "first_name": "Jo", "last_name": "Doe"}]
    with pytest.raises(skip_trace.TracerfyError) as exc:
        skip_trace.submit_batch(rows, api_token="tok")

    assert tracerfy_points_at.accepted == 0
    # Same verdict, and so the same dispatcher handling, as a refusal at
    # validation time: the rows are errored, never retried against a blocked host.
    assert str(exc.value).startswith("Refusing unsafe Tracerfy endpoint")
    assert classify_submit_failure(str(exc.value)) == "provider_error"


def test_tracerfy_queue_list_never_sends_the_token_to_an_internal_address(
    tracerfy_points_at, precheck_says_public,
):
    with pytest.raises(skip_trace.TracerfyError) as exc:
        skip_trace.fetch_queues(api_token="tok")

    assert tracerfy_points_at.accepted == 0
    assert str(exc.value).startswith("Refusing unsafe Tracerfy endpoint")


def test_the_listener_really_accepts_connections(internal_service):
    """Positive control: without pinning, a socket to the listener does connect, so
    `accepted == 0` above means "refused", not "listener unreachable"."""
    with socket.create_connection(("127.0.0.1", internal_service.port), timeout=2):
        pass
    for _ in range(20):
        if internal_service.accepted:
            break
        threading.Event().wait(0.05)
    assert internal_service.accepted == 1
