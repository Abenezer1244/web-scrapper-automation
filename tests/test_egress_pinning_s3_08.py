"""S3-08 (audit #3, re-confirmed audit #5): server-side fetches go only to the host
that was validated, at an address the SSRF policy approved.

Two gaps, both closed here:
  * parser differential: urlparse and requests disagree on where the authority ends
    when it carries a backslash or userinfo, so the host validated was not the host
    dialled;
  * DNS rebinding: ``safe_http`` and the dialer outbox validated (resolving the
    host) and then let ``requests`` resolve it again to connect.

Real sockets against a real local server, which is exactly the "internal service" a
rebinding attacker aims at. Where a test needs the pre-check to have said "public"
(the rebinding attacker's first answer), it replaces only that pre-check, the same
seam tests/test_webhook_dns_pinning.py uses; the transport is production.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from src.api.middleware.security import validate_scraping_target
from src.utils import safe_http
from src.workers import dialer_outbox
from src.workers.dialer_connectors.phoneburner import PhoneBurnerConnector


class _Recorder(BaseHTTPRequestHandler):
    hits: list[str] = []

    def _answer(self):
        _Recorder.hits.append(f"{self.command} {self.path}")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Length", "8")
        self.end_headers()
        self.wfile.write(b"internal")

    do_GET = _answer  # noqa: N815 (http.server API)
    do_POST = _answer  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture
def internal_service():
    """A local service standing in for an internal endpoint. Yields its port."""
    _Recorder.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def precheck_says_public(monkeypatch):
    """The rebinding attacker's first DNS answer: validation passes."""
    monkeypatch.setattr(safe_http, "validate_scraping_target", lambda *a, **k: None)


# --- parser differential ------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://evil.example\\@portal.county.gov/records",
    "https://evil.example\\@portal.county.gov/",
    "https://user:pw@portal.county.gov/",
    "https://portal.county.gov@evil.example/",
])
def test_a_url_whose_host_parsers_disagree_on_is_refused(url):
    with pytest.raises(ValueError, match="userinfo or a backslash"):
        validate_scraping_target(url, require_allowlisted=False, resolve=False)


def test_the_differential_is_real_for_requests():
    """Why the rule exists: requests dials a different host than urlparse names."""
    from urllib.parse import urlparse

    import requests
    from urllib3.util import parse_url

    url = "http://evil.example\\@portal.county.gov/x"
    assert urlparse(url).hostname == "portal.county.gov"
    assert parse_url(requests.Request("GET", url).prepare().url).host == "evil.example"


def test_a_backslash_in_the_path_or_query_is_still_allowed():
    validate_scraping_target(
        "https://portal.county.gov/search\\x?name=a\\b", require_allowlisted=False, resolve=False
    )


# --- DNS rebinding: safe_http --------------------------------------------------------

def test_safe_get_refuses_an_internal_address_at_connect(internal_service, precheck_says_public):
    with pytest.raises(ValueError, match="not permitted"):
        safe_http.safe_get(f"http://127.0.0.1:{internal_service}/admin")
    assert _Recorder.hits == []


def test_safe_get_following_refuses_an_internal_address_at_connect(
    internal_service, precheck_says_public
):
    with pytest.raises(ValueError, match="not permitted"):
        safe_http.safe_get_following(f"http://localhost:{internal_service}/admin")
    assert _Recorder.hits == []


def test_safe_download_refuses_an_internal_address_at_connect(
    internal_service, precheck_says_public, tmp_path
):
    with pytest.raises(ValueError, match="not permitted"):
        safe_http.safe_download_to_file(
            f"http://127.0.0.1:{internal_service}/dump", str(tmp_path / "out.bin"), max_bytes=1024,
            require_https=False,
        )
    assert _Recorder.hits == []
    assert not (tmp_path / "out.bin").exists()


def test_safe_get_still_fetches_an_approved_address(internal_service, precheck_says_public, monkeypatch):
    """Positive control: with loopback admitted (and only loopback), the pinned
    transport fetches normally, so the refusals above are the policy, not a
    broken transport."""
    from src.utils.pinned_http import pinned_session
    from tests.test_webhook_dns_pinning import _loopback_adapter

    monkeypatch.setattr(safe_http, "_SESSION", pinned_session(_loopback_adapter()))
    resp = safe_http.safe_get(f"http://127.0.0.1:{internal_service}/ok")
    assert resp.status_code == 200
    assert resp.content == b"internal"
    assert _Recorder.hits == ["GET /ok"]


# --- DNS rebinding: dialer outbox ----------------------------------------------------

class _LoopbackVendor(PhoneBurnerConnector):
    """The real connector, its pinned vendor host moved to the local service."""

    ALLOWED_HOSTS = frozenset({"127.0.0.1"})


def _row():
    return SimpleNamespace(
        id="r1", status="pending", last_error=None,
        vendor_response_code=None, vendor_contact_id=None, delivered_at=None,
    )


def test_dialer_outbox_refuses_an_internal_address_at_connect(internal_service, monkeypatch):
    # The pre-check passes, as it would for the vendor host's public first answer.
    monkeypatch.setattr(dialer_outbox, "validate_outbound_webhook", lambda url: None)
    row = _row()
    req = {
        "url": f"http://127.0.0.1:{internal_service}/rest/1/contacts",
        "headers": {"Authorization": "Bearer not-a-real-token"},
        "body": {"first_name": "A", "phone": "4255551212"},
    }
    dialer_outbox._deliver_one(_LoopbackVendor(), req, row)
    assert _Recorder.hits == []  # no PII, no bearer token left the box
    assert row.status == "failed"
    assert row.last_error == "blocked by SSRF guard"
    assert row.vendor_response_code is None
