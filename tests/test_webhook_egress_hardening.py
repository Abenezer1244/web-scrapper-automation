"""Customer webhook egress hardening (audit 2026-09-25: E-1, E-2, E-3).

E-1: the webhook POST read the whole response body into memory, so a
customer-chosen endpoint could return a gzip bomb and OOM-kill a worker shared
with every tenant's scrapes. E-2: a network error logged ``str(exc)``, which for
requests carries the full URL, query-string secret included. E-3: the SSRF
blocklist unwrapped only ``::ffff:``-mapped IPv6, so NAT64, 6to4, Teredo and
IPv4-compatible forms of a blocked IPv4 address passed.

The webhook tests talk to a REAL local HTTP server. The only seam is the SSRF
guard, patched to let 127.0.0.1 through: the guard itself is covered below and
in test_webhook_ssrf.py.
"""
from __future__ import annotations

import gzip
import ipaddress
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.api.middleware.security import _ip_is_blocked
from src.workers import webhook_delivery as wd

_BOMB = gzip.compress(b"\0" * (64 * 1024 * 1024))  # ~64 KB on the wire, 64 MB decoded


class _Handler(BaseHTTPRequestHandler):
    sent = 0

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.startswith("/bomb"):
            body = _BOMB
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
        elif self.path.startswith("/echo"):  # an endpoint that echoes our request line
            body = f"bad request: {self.path}".encode()
            self.send_response(400)
        else:  # /flood: a large plain body
            body = b"x" * (32 * 1024 * 1024)
            self.send_response(500)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i:i + 64 * 1024])
                _Handler.sent += 64 * 1024
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def local_endpoint(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(wd, "validate_outbound_webhook", lambda url: None)
    _Handler.sent = 0
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_gzip_bomb_is_not_decoded_into_memory(local_endpoint):
    result = wd.deliver_job_webhook.apply(
        args=("job-bomb", f"{local_endpoint}/bomb", {"event": "job.completed"})
    ).get()
    assert result["status"] == "delivered"
    # Compressed despite Accept-Encoding: identity, so it is never decompressed.
    assert result["response_excerpt"] == "<gzip body, not decoded>"


def test_a_huge_error_body_is_not_read_in_full(local_endpoint):
    wd.deliver_job_webhook.apply(
        args=("job-flood", f"{local_endpoint}/flood", {"event": "job.completed"}),
        retries=wd._MAX_RETRIES,  # final attempt: no retry loop in the test
    )
    # The server stops being read once the excerpt is taken; it must not have
    # been able to push anything like the full 32 MB into the worker.
    assert _Handler.sent < 8 * 1024 * 1024


def test_a_network_error_never_logs_the_url_secret(monkeypatch, caplog):
    monkeypatch.setattr(wd, "validate_outbound_webhook", lambda url: None)
    # Nothing listens on port 9 (discard) locally: connection refused.
    url = "http://127.0.0.1:9/hooks/catch?token=SUPERSECRETVALUE123"
    with caplog.at_level(logging.DEBUG):
        wd.deliver_job_webhook.apply(
            args=("job-down", url, {"event": "job.completed"}),
            retries=wd._MAX_RETRIES,
        )
    assert "SUPERSECRETVALUE123" not in caplog.text
    assert "/hooks/catch" not in caplog.text


def test_an_echoed_url_secret_is_scrubbed_from_excerpt_and_logs(local_endpoint, caplog):
    url = f"{local_endpoint}/echo?token=ECHOEDSECRETVALUE456"
    # INFO is the production worker level (start.sh --loglevel=info). urllib3's
    # own DEBUG connection log prints the raw request line; nothing here can
    # scrub a library's DEBUG output, so it must never be enabled in prod.
    with caplog.at_level(logging.INFO):
        result = wd.deliver_job_webhook.apply(
            args=("job-echo", url, {"event": "job.completed"}),
            retries=wd._MAX_RETRIES,
        ).get()
    assert result["status"] == "failed" and result["status_code"] == 400
    assert "bad request" in result["response_excerpt"]  # the excerpt is still useful
    assert "ECHOEDSECRETVALUE456" not in result["response_excerpt"]
    assert "ECHOEDSECRETVALUE456" not in caplog.text


def test_a_path_secret_echoed_back_is_scrubbed(local_endpoint):
    # Catch-hook services carry the secret in the PATH, not the query.
    url = f"{local_endpoint}/echo/hooks/catch/123456/PATHSECRETabc789/"
    result = wd.deliver_job_webhook.apply(
        args=("job-path", url, {"event": "job.completed"}), retries=wd._MAX_RETRIES,
    ).get()
    assert "PATHSECRETabc789" not in result["response_excerpt"]


def test_a_percent_encoded_query_secret_is_scrubbed_in_its_raw_form():
    url = "https://hooks.example.com/in?token=ab%2Bcd%2Fef"
    assert "ab%2Bcd%2Fef" not in wd._redact_url_secrets("echo: token=ab%2Bcd%2Fef", url)
    assert "ab+cd/ef" not in wd._redact_url_secrets("echo: token=ab+cd/ef", url)


def test_a_secret_cut_off_at_the_excerpt_boundary_is_not_left_half_visible():
    url = "https://hooks.example.com/in?token=TRUNCATEDSECRET999"
    text = "x" * 50 + "token=TRUNCATEDSEC"  # the read stopped mid-secret
    assert "TRUNCATEDSEC" not in wd._redact_url_secrets(text, url)


@pytest.mark.parametrize("addr", [
    "::ffff:0:7f00:1",                         # SIIT IPv4-translated 127.0.0.1
    "64:ff9b::a9fe:a9fe",                      # NAT64 of 169.254.169.254
    "64:ff9b:1::a9fe:a9fe",                    # local-use NAT64 prefix
    "2002:a9fe:a9fe::1",                       # 6to4 of 169.254.169.254
    "::a9fe:a9fe",                             # IPv4-compatible 169.254.169.254
    "::7f00:1",                                # IPv4-compatible 127.0.0.1
    "2001:0:4136:e378:8000:63bf:80ff:fffe",    # Teredo, client 127.0.0.1
    "fec0::1",                                 # deprecated site-local
    "192.88.99.1",                             # 6to4 relay anycast
])
def test_ipv4_embedding_forms_of_blocked_addresses_are_blocked(addr):
    assert _ip_is_blocked(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"])
def test_public_addresses_stay_allowed(addr):
    # Positive control: NAT64 of a PUBLIC address (8.8.8.8) is still public.
    assert not _ip_is_blocked(ipaddress.ip_address(addr))
