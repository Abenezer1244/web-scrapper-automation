"""P0-02 ops heartbeat: the ping that a dead beat, worker or broker stops sending.

A REAL HTTP server on 127.0.0.1 receives the GET, so the request on the wire, the
status handling, the redirect refusal and the log hygiene are all exercised for real.
Nothing is mocked; only the timeout constant is shortened for the slow-server case.
"""
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.config import settings
from src.workers import ops_heartbeat
from src.workers.ops_heartbeat import _allowed, ping_heartbeat
from src.workers.scheduler import app
from src.workers.scheduler import ops_heartbeat as ops_heartbeat_task

_TOKEN = "secret-token-p002"
_PATH = f"/api/v1/heartbeat/{_TOKEN}"


@pytest.fixture
def heartbeat_server():
    """Yields (url, received_paths, state); state holds the status and a reply delay."""
    received: list[str] = []
    state = {"status": 200, "delay": 0.0}

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            received.append(self.path)
            if state["delay"]:
                time.sleep(state["delay"])
            self.send_response(state["status"])
            if 300 <= state["status"] < 400:
                self.send_header("Location", "/followed")  # a followed redirect would arrive here
            self.end_headers()

        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}{_PATH}", received, state
    finally:
        srv.shutdown()
        srv.server_close()


def test_off_by_default_sends_nothing():
    assert settings.OPS_HEARTBEAT_URL == ""
    assert ping_heartbeat() is False


def test_a_ping_is_one_get_and_a_2xx_counts(monkeypatch, heartbeat_server):
    url, received, _ = heartbeat_server
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", url)

    assert ping_heartbeat() is True
    assert received == [_PATH]


def test_the_beat_task_returns_the_ping_result(monkeypatch, heartbeat_server):
    url, received, _ = heartbeat_server
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", url)

    assert ops_heartbeat_task() is True
    assert received == [_PATH]


def test_a_rejected_ping_is_false_and_the_url_is_never_logged(monkeypatch, heartbeat_server, caplog):
    """The URL can fake liveness. A 500 must come back False and leave the URL out of
    every log line at any level, DEBUG included."""
    url, received, state = heartbeat_server
    state["status"] = 500
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", url)

    with caplog.at_level(logging.DEBUG):
        assert ping_heartbeat() is False

    assert received == [_PATH]
    assert "status=500" in caplog.text
    assert _TOKEN not in caplog.text


def test_a_redirect_is_refused_not_followed(monkeypatch, heartbeat_server, caplog):
    url, received, state = heartbeat_server
    state["status"] = 302
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", url)

    with caplog.at_level(logging.DEBUG):
        assert ping_heartbeat() is False

    assert received == [_PATH]  # nothing arrived at /followed
    assert _TOKEN not in caplog.text


def test_an_unreachable_monitor_is_false_never_raises_never_logs_the_url(monkeypatch, caplog):
    with socket.socket() as s:  # a port nothing listens on once this closes
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", f"http://127.0.0.1:{port}{_PATH}")

    with caplog.at_level(logging.DEBUG):
        assert ping_heartbeat() is False

    assert "ops heartbeat failed" in caplog.text
    assert _TOKEN not in caplog.text


def test_a_hung_monitor_times_out_instead_of_stacking_pings(monkeypatch, heartbeat_server, caplog):
    url, received, state = heartbeat_server
    state["delay"] = 2.0
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", url)
    monkeypatch.setattr(ops_heartbeat, "_HEARTBEAT_TIMEOUT_SECONDS", 0.3)

    started = time.monotonic()
    with caplog.at_level(logging.DEBUG):
        assert ping_heartbeat() is False
    assert time.monotonic() - started < 1.5
    assert _TOKEN not in caplog.text


def test_plain_http_off_the_machine_is_refused_before_any_request(monkeypatch, caplog):
    # .invalid never resolves (RFC 2606); the guard must refuse before trying.
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", f"http://heartbeat.invalid{_PATH}")

    with caplog.at_level(logging.DEBUG):
        assert ping_heartbeat() is False

    assert "must be an https URL" in caplog.text
    assert "ops heartbeat failed" not in caplog.text  # refused, not attempted
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        # urlsplit raises on both, and the NFKC error message quotes the netloc (Codex P2).
        f"https://[::1{_TOKEN}/x",
        f"https://{_TOKEN}／evil.example/x",
    ],
)
def test_a_malformed_url_is_refused_without_raising_or_logging_it(monkeypatch, caplog, url):
    monkeypatch.setattr(settings, "OPS_HEARTBEAT_URL", url)

    with caplog.at_level(logging.DEBUG):
        assert ping_heartbeat() is False  # returned, not raised into the Celery task

    assert "must be an https URL" in caplog.text
    assert _TOKEN not in caplog.text


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://uptime.betterstack.com/api/v1/heartbeat/x", True),
        ("https://", False),
        ("http://127.0.0.1:8080/x", True),
        ("http://localhost/x", True),
        ("http://[::1]/x", True),
        ("http://10.0.0.5/x", False),
        ("http://uptime.betterstack.com/x", False),
        ("ftp://uptime.betterstack.com/x", False),
        ("not a url", False),
    ],
)
def test_only_https_or_loopback_http_is_allowed(url, ok):
    assert _allowed(url) is ok


def test_the_beat_entry_pings_every_5_minutes_and_drops_stale_pings():
    entry = app.conf.beat_schedule["ops-heartbeat"]
    assert entry["task"] == "src.workers.scheduler.ops_heartbeat"
    assert entry["task"] in app.tasks
    # Under 10 minutes stays an interval (tests/test_beat_schedule.py); the Better Stack
    # monitor is configured for a 5-minute period plus 5 minutes of grace.
    assert entry["schedule"] == 300.0
    # A ping older than its expiry proves a backlog, not a live worker; the expiry must
    # also outlast one slow ping so a healthy worker never drops its own heartbeat.
    assert ops_heartbeat._HEARTBEAT_TIMEOUT_SECONDS < entry["options"]["expires"] < entry["schedule"]
