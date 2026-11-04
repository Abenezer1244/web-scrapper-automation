"""P0-10: the hourly Tracerfy balance check and its low-balance page.

fetch_balance runs over REAL HTTPS against a local server with a freshly generated
certificate: real requests, TLS, JSON parsing and the pinned transport. Two policy
seams are narrowed for that one host and port only, as test_webhook_dns_pinning
does: the SSRF validator admits it (every other URL still goes to the real one)
and the pinned session may dial loopback (that file's _loopback_adapter). The
rebinding test runs the UNMODIFIED production policy. The check-level tests record
what would be paged; delivery itself is test_ops_alerts' job and the deploy drill's.
"""
import datetime
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from celery.schedules import crontab

from src.config import settings
from src.workers import tracerfy_balance as tb
from src.workers.scheduler import app
from tests.test_egress_pinning_5b_ii import _Listener
from tests.test_webhook_dns_pinning import _loopback_adapter

_TOKEN = "tok-p010-sentinel"
_BODY_SENTINEL = "bodysentinelp010"


class _Tracerfy(BaseHTTPRequestHandler):
    reply: tuple[int, bytes, dict] = (200, b"{}", {})
    delay = 0.0
    hits: list[dict] = []

    def do_GET(self):  # noqa: N802 (http.server API)
        _Tracerfy.hits.append({"path": self.path, "auth": self.headers.get("Authorization")})
        time.sleep(_Tracerfy.delay)
        status, body, headers = _Tracerfy.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem, key_pem = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return cert_pem, key_pem


@pytest.fixture
def tracerfy(tmp_path, monkeypatch):
    """A local HTTPS Tracerfy. Yields a function that sets the next reply."""
    from src.utils.pinned_http import pinned_session

    cert_pem, key_pem = _cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_pem, key_pem)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Tracerfy)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"https://localhost:{server.server_address[1]}"
    real_validate = tb.validate_scraping_target

    def validate(url, **kwargs):
        if url.startswith(base + "/"):
            return None  # only this test host skips the address policy
        return real_validate(url, **kwargs)

    def session():
        s = pinned_session(_loopback_adapter())  # may dial loopback, nothing else private
        s.verify = str(cert_pem)
        return s

    monkeypatch.setattr(tb, "validate_scraping_target", validate)
    monkeypatch.setattr(tb, "pinned_session", session)
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", base)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", _TOKEN)
    _Tracerfy.hits = []
    _Tracerfy.delay = 0.0

    def reply(status=200, body=None, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        _Tracerfy.reply = (status, raw, headers or {})

    yield reply
    server.shutdown()


_GOOD = {"total_queues": 12, "properties_traced": 18350, "queues_pending": 2,
         "queues_completed": 10, "balance": 940, "meta": {"request_id": "req_x"}}


# ─── fetch_balance over real HTTPS ───────────────────────────────────────────

def test_reads_the_balance_with_the_bearer_token(tracerfy):
    tracerfy(200, _GOOD)
    assert tb.fetch_balance() == 940
    assert _Tracerfy.hits == [{"path": "/v1/api/analytics/", "auth": f"Bearer {_TOKEN}"}]


def test_zero_is_a_real_balance(tracerfy):
    tracerfy(200, {**_GOOD, "balance": 0})
    assert tb.fetch_balance() == 0


@pytest.mark.parametrize("balance", [True, False, "940", 940.0, -1, None])
def test_a_balance_that_is_not_a_non_negative_int_is_malformed(tracerfy, balance):
    tracerfy(200, {**_GOOD, "balance": balance})
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance()
    assert exc.value.category == tb.MALFORMED


@pytest.mark.parametrize("body", [{"total_queues": 1}, [{"balance": 9}], b"not json", b""])
def test_a_body_without_a_balance_is_malformed(tracerfy, body):
    tracerfy(200, body)
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance()
    assert exc.value.category == tb.MALFORMED


@pytest.mark.parametrize(("status", "category"), [
    (401, tb.UNAUTHORIZED), (403, tb.UNAUTHORIZED), (429, tb.TRANSIENT), (500, tb.TRANSIENT),
    (503, tb.TRANSIENT), (404, tb.MALFORMED), (204, tb.MALFORMED),
])
def test_status_categories(tracerfy, status, category):
    tracerfy(status, {"balance": 5, "error": _BODY_SENTINEL})
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance()
    assert exc.value.category == category
    assert _BODY_SENTINEL not in str(exc.value)


def test_a_redirect_is_never_followed_even_with_a_plausible_body(tracerfy):
    tracerfy(301, {"balance": 99999}, {"Location": "https://localhost:1/v1/api/analytics/"})
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance()
    assert exc.value.category == tb.MALFORMED
    assert len(_Tracerfy.hits) == 1


def test_a_slow_answer_is_transient(tracerfy):
    tracerfy(200, _GOOD)
    _Tracerfy.delay = 2.0
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance(timeout=0.5)
    assert exc.value.category == tb.TRANSIENT


def test_nothing_listening_is_transient(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the request: connection refused
    from src.utils.pinned_http import pinned_session

    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", _TOKEN)
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", f"https://localhost:{port}")
    monkeypatch.setattr(tb, "validate_scraping_target", lambda *a, **k: None)
    monkeypatch.setattr(tb, "pinned_session", lambda: pinned_session(_loopback_adapter()))
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance(timeout=2)
    assert exc.value.category == tb.TRANSIENT


def test_plain_http_and_a_missing_token_never_leave_the_box(monkeypatch):
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", _TOKEN)
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", "http://tracerfy.com")
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance()
    assert exc.value.category == tb.UNSAFE_ENDPOINT
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "")
    with pytest.raises(tb.BalanceError) as exc:
        tb.fetch_balance()
    assert exc.value.category == tb.NOT_CONFIGURED


# ─── the production policy, unmodified ───────────────────────────────────────

_REBIND_HOST = "rebind-p010.bridgeleads-test.example"


def test_the_token_never_reaches_an_internal_address_through_rebinding(monkeypatch):
    """The real validator sees a public answer, any later lookup gets loopback (a
    TTL-0 rebinding record). The pinned transport must refuse to dial it."""
    listener = _Listener()
    real_getaddrinfo = socket.getaddrinfo
    real_validate = tb.validate_scraping_target
    phase = {"validating": False, "validated": 0}

    def getaddrinfo(host, port, *args, **kwargs):
        if host != _REBIND_HOST:
            return real_getaddrinfo(host, port, *args, **kwargs)
        ip = "93.184.216.34" if phase["validating"] else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    def validate(*args, **kwargs):
        phase["validating"] = True
        try:
            result = real_validate(*args, **kwargs)
            phase["validated"] += 1
            return result
        finally:
            phase["validating"] = False

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(tb, "validate_scraping_target", validate)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", _TOKEN)
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", f"https://{_REBIND_HOST}:{listener.port}")
    try:
        with pytest.raises(tb.BalanceError) as exc:
            tb.fetch_balance(timeout=5)
    finally:
        listener.close()
    assert phase["validated"] == 1  # the real check ran and passed
    assert listener.accepted == 0
    assert exc.value.category == tb.UNSAFE_ENDPOINT


# ─── check_tracerfy_balance: gates, threshold, pages ─────────────────────────

@pytest.fixture
def paged(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(tb, "send_ops_alert", lambda *args: sent.append(args) or True)
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_LOW_BALANCE_CREDITS", 1000)
    return sent


@pytest.mark.parametrize("gate", ["threshold", "disabled", "token"])
def test_off_makes_no_call(tracerfy, paged, monkeypatch, gate):
    tracerfy(200, {**_GOOD, "balance": 1})
    if gate == "threshold":
        monkeypatch.setattr(settings, "TRACERFY_LOW_BALANCE_CREDITS", 0)
    elif gate == "disabled":
        monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)
    else:
        monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "")
    assert tb.check_tracerfy_balance() == "off"
    assert _Tracerfy.hits == [] and paged == []


def test_below_the_threshold_pages_on_its_own_bucket(tracerfy, paged):
    tracerfy(200, {**_GOOD, "balance": 999})
    assert tb.check_tracerfy_balance() == "low"
    (kind, key, subject, body), = paged
    assert (kind, key) == ("tracerfy_low_balance", "account")
    assert "999" in subject and "1,000" in body
    assert (kind, key) != ("skip_trace", "out_of_credits")  # never suppresses the 402 page


def test_at_the_threshold_is_not_low(tracerfy, paged):
    tracerfy(200, {**_GOOD, "balance": 1000})
    assert tb.check_tracerfy_balance() == "ok"
    assert paged == []


@pytest.mark.parametrize(("status", "body", "kind"), [
    (401, {"error": _BODY_SENTINEL}, "tracerfy_account"),
    (403, {"error": "api_disabled", "detail": _BODY_SENTINEL}, "tracerfy_account"),
    (200, {"balance": True, "note": _BODY_SENTINEL}, "tracerfy_balance_monitor"),
    (302, {"balance": 5}, "tracerfy_balance_monitor"),
])
def test_refused_key_and_broken_monitor_page(tracerfy, paged, caplog, status, body, kind):
    tracerfy(status, body)
    tb.check_tracerfy_balance()
    (sent_kind, key, subject, text), = paged
    assert (sent_kind, key) == (kind, "account")
    for leak in (_TOKEN, _BODY_SENTINEL):
        assert leak not in subject + text and leak not in caplog.text


@pytest.mark.parametrize("status", [429, 502, 503])
def test_transient_failures_wait_for_the_next_tick(tracerfy, paged, status):
    tracerfy(status, {"error": _BODY_SENTINEL})
    assert tb.check_tracerfy_balance() == tb.TRANSIENT
    assert paged == []


def test_the_threshold_cannot_be_negative():
    from pydantic import ValidationError

    from src.config.settings import Settings

    with pytest.raises(ValidationError):
        Settings(TRACERFY_LOW_BALANCE_CREDITS=-1)


def test_beat_runs_the_check_hourly_through_the_scheduler_task():
    entry = app.conf.beat_schedule["tracerfy-balance-check"]
    assert entry["task"] == "src.workers.scheduler.tracerfy_balance_check"
    assert isinstance(entry["schedule"], crontab) and entry["schedule"].minute == {52}
    assert len(entry["schedule"].hour) == 24
    assert entry["task"] in app.tasks
