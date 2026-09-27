"""F-03 (audit 2026-09-25): the webhook socket goes only to an address the SSRF
policy approved, resolved in the same step, so DNS rebinding between the
pre-check and the POST cannot reach an internal address.

Every test uses real sockets against a real local server. A loopback server is
exactly the "internal service" a rebinding attack would aim at, so the refusal
tests need nothing else. The positive tests admit loopback, and loopback only,
through LoopbackOnlyAdapter; everything else about the transport is production.

The pinned_http imports sit inside the tests that need them so this module
still collects on the pre-fix code, where the first tests must FAIL, not error.
"""
import datetime
import ipaddress
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from src.workers import webhook_delivery as wd


class _Recorder(BaseHTTPRequestHandler):
    hits: list[dict] = []

    def do_POST(self):  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Recorder.hits.append({"host": self.headers.get("Host"), "peer": self.client_address[0]})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def internal_service():
    """A local service standing in for an internal endpoint. Yields its port."""
    _Recorder.hits = []
    server = _serve(ThreadingHTTPServer(("127.0.0.1", 0), _Recorder))
    yield server.server_address[1]
    server.shutdown()


def _loopback_adapter():
    from src.api.middleware.security import _ip_is_blocked
    from src.utils.pinned_http import PinnedAdapter, _PinnedHTTPPool, _PinnedHTTPSPool

    def is_blocked(addr):
        return False if addr.is_loopback else _ip_is_blocked(addr)

    http_conn = type("LoopbackHTTP", (_PinnedHTTPPool.ConnectionCls,), {"_is_blocked": staticmethod(is_blocked)})
    https_conn = type("LoopbackHTTPS", (_PinnedHTTPSPool.ConnectionCls,), {"_is_blocked": staticmethod(is_blocked)})
    http_pool = type("LoopbackHTTPPool", (_PinnedHTTPPool,), {"ConnectionCls": http_conn})
    https_pool = type("LoopbackHTTPSPool", (_PinnedHTTPSPool,), {"ConnectionCls": https_conn})
    return type("LoopbackOnlyAdapter", (PinnedAdapter,), {"pool_classes": {"http": http_pool, "https": https_pool}})


# --- refusal: these FAIL on the pre-fix transport, which simply connects -----

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_the_webhook_session_refuses_an_internal_address_at_connect(internal_service, host):
    # No pre-check runs here: this is the transport alone, i.e. the state a
    # rebinding attacker reaches after validate_outbound_webhook said "public".
    with pytest.raises(requests.ConnectionError):
        wd._SESSION.post(f"http://{host}:{internal_service}/hook", json={}, timeout=5)
    assert _Recorder.hits == []


def test_a_rebind_after_the_precheck_is_blocked_not_delivered_or_retried(internal_service, monkeypatch):
    # The pre-check passes (as it would for a public first answer); the connect
    # then meets an internal address. The task must refuse it, send nothing, and
    # return "blocked" on the first attempt instead of raising for autoretry.
    monkeypatch.setattr(wd, "validate_outbound_webhook", lambda url: None)
    result = wd.deliver_job_webhook.apply(
        args=("job-rebind", f"http://127.0.0.1:{internal_service}/hook", {"event": "job.completed"})
    ).get()
    assert result["status"] == "blocked"
    assert result["attempts"] == 1
    assert _Recorder.hits == []


# --- the classifier and the transport's own guarantees ---------------------------

def test_a_connect_time_block_is_recognised_through_requests_wrapping(internal_service):
    from src.utils.pinned_http import is_blocked_destination

    with pytest.raises(requests.ConnectionError) as blocked:
        wd._SESSION.post(f"http://127.0.0.1:{internal_service}/", json={}, timeout=5)
    assert is_blocked_destination(blocked.value)

    with pytest.raises(requests.ConnectionError) as refused:
        requests.post("http://127.0.0.1:9/", timeout=5)  # plain refusal, nothing listening
    assert not is_blocked_destination(refused.value)


def test_proxies_are_refused_even_when_passed_explicitly():
    with pytest.raises(ValueError, match="Proxies are not permitted"):
        wd._SESSION.post("https://example.com/", proxies={"https": "http://127.0.0.1:3128"}, timeout=5)


def test_a_private_address_is_refused_before_any_packet_is_sent():
    from src.utils.pinned_http import pinned_session

    # 10.255.255.1 is unroutable here: had the transport tried to connect, the
    # error would be a timeout, not the policy refusal asserted below.
    session = pinned_session(_loopback_adapter())
    with pytest.raises(requests.ConnectionError, match="blocked address"):
        session.post("http://10.255.255.1:81/", json={}, timeout=5)


# CPython raises these audit events for every real lookup and connect, which lets
# a test observe the resolver without replacing it. The hook cannot be removed,
# so it records only while a test holds the list.
_AUDIT: list | None = None


def _audit(event, args):
    if _AUDIT is not None and event in ("socket.getaddrinfo", "socket.connect"):
        _AUDIT.append((event, args))


sys.addaudithook(_audit)


def test_one_lookup_per_connection_and_the_socket_goes_to_its_answer(internal_service):
    # A transport that checks one lookup and then connects by hostname would do
    # a SECOND lookup, and that second answer is what a rebinding DNS server
    # controls. Exactly one lookup, with the connect aimed at its answer, is
    # the property that closes F-03.
    global _AUDIT
    from src.utils.pinned_http import pinned_session

    session = pinned_session(_loopback_adapter())
    _AUDIT = []
    try:
        resp = session.post(f"http://localhost:{internal_service}/hook", json={}, timeout=5)
    finally:
        events, _AUDIT = _AUDIT, None
    assert resp.status_code == 200

    lookups = [a for e, a in events if e == "socket.getaddrinfo" and a[0] == "localhost"]
    connects = [a[1] for e, a in events if e == "socket.connect"]
    answer = {info[4] for info in socket.getaddrinfo("localhost", internal_service, type=socket.SOCK_STREAM)}
    assert len(lookups) == 1, lookups
    assert connects and all(addr in answer for addr in connects), (connects, answer)


# --- positive path: pinning must not change what the server sees ----------------

def test_the_host_header_keeps_the_configured_hostname(internal_service):
    from src.utils.pinned_http import pinned_session

    resp = pinned_session(_loopback_adapter()).post(
        f"http://localhost:{internal_service}/hook", json={"a": 1}, timeout=5
    )
    assert resp.status_code == 200
    assert _Recorder.hits == [{"host": f"localhost:{internal_service}", "peer": "127.0.0.1"}]


def test_tls_still_sends_sni_and_verifies_the_certificate_against_the_hostname(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from src.utils.pinned_http import pinned_session

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        # DNS name only, no IP SAN: verification can only succeed by hostname.
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))

    sni_seen: list[str | None] = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_pem, key_pem)
    ctx.sni_callback = lambda sock, server_name, _ctx: sni_seen.append(server_name)

    _Recorder.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    _serve(server)
    port = server.server_address[1]
    try:
        session = pinned_session(_loopback_adapter())
        resp = session.post(f"https://localhost:{port}/hook", json={}, timeout=5, verify=str(cert_pem))
        assert resp.status_code == 200
        assert sni_seen == ["localhost"]
        assert _Recorder.hits[0]["peer"] == "127.0.0.1"
        # Same socket target, but the name no longer matches the certificate:
        # verification is still bound to the hostname, not to the pinned IP.
        with pytest.raises(requests.exceptions.SSLError):
            session.post(f"https://127.0.0.1:{port}/hook", json={}, timeout=5, verify=str(cert_pem))
    finally:
        server.shutdown()


def test_loopback_adapter_admits_loopback_only():
    # Guards the test double itself: it may widen the policy to loopback and no further.
    adapter = _loopback_adapter()
    conn_cls = adapter.pool_classes["https"].ConnectionCls
    assert not conn_cls._is_blocked(ipaddress.ip_address("127.0.0.1"))
    for addr in ("10.0.0.1", "169.254.169.254", "192.168.1.1", "::ffff:169.254.169.254"):
        assert conn_cls._is_blocked(ipaddress.ip_address(addr)), addr
