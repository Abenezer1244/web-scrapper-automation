"""M6 ops alerts: configuration gates. Never sends a real email.

The send path is exercised in prod by construction (best-effort + cooldown);
these lock the SAFE defaults: e-mail DELIVERY is a no-op unless explicitly
configured, and a missing Resend key can never raise out of a worker task.

NOTE (2026-09-04): "no-op" now means *no e-mail*, not *no side effect*. Every
alert-worthy call also writes a durable audit_events row — inline for a sync
caller like these tests, handed to an executor only when an event loop is running
— because OPS_ALERT_EMAIL was blank in production and the old silent return meant
a four-week crawl outage left no trace anywhere. The autouse fixture below stubs
that write so these stay pure; the behaviour itself is covered in
tests/test_nts_upsert_and_ops_alert_durability.py.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.config import settings
from src.workers import ops_alerts
from src.workers.ops_alerts import send_ops_alert


@pytest.fixture(autouse=True)
def _no_real_audit_write(monkeypatch):
    """Every send now also records a durable audit row. These tests are about the
    CONFIG GATES, so stub the persistence seam — otherwise each one would attempt a
    real INSERT against whatever database the suite is pointed at (Codex)."""
    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def add(self, _obj):
            pass

        def commit(self):
            pass

    monkeypatch.setattr(ops_alerts, "_session_for_persist", lambda: _S())


def test_disabled_by_default_is_silent_noop():
    # Dev/CI default: OPS_ALERT_EMAIL empty -> returns False, sends nothing,
    # raises nothing (the watchdog/canary callers depend on this contract).
    assert settings.OPS_ALERT_EMAIL == ""
    assert send_ops_alert("canary", "x/WA", "subject", "body") is False


def test_never_raises_even_with_weird_input():
    # Best-effort contract: garbage in -> False out, no exception.
    assert send_ops_alert("", "", "", "") is False
    assert send_ops_alert("k" * 500, "y" * 500, "s" * 500, "<b>&</b>" * 100) is False


def test_configured_send_failure_never_raises(monkeypatch):
    """Resend (external API — mock justified) blowing up must return False,
    never raise into the watchdog/canary caller (Codex P2 coverage gap)."""
    import sys
    import types

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.bridgeleads.io")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    # Cooldown: force-acquired so the send path is reached without Redis.
    import src.workers.ops_alerts as oa
    monkeypatch.setattr(oa, "_cooldown_acquired", lambda *_a: True)

    fake = types.ModuleType("resend")
    class _Emails:
        @staticmethod
        def send(_payload):
            raise RuntimeError("resend exploded")
    fake.Emails = _Emails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)

    assert send_ops_alert("canary", "x/WA", "subject", "body") is False  # not raised


def test_configured_send_success_and_cooldown_suppression(monkeypatch):
    """Happy path returns True and the payload is well-formed; a cooldown-held
    bucket suppresses the send entirely."""
    import sys
    import types

    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.bridgeleads.io")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    import src.workers.ops_alerts as oa

    sent: list[dict] = []
    fake = types.ModuleType("resend")
    class _Emails:
        @staticmethod
        def send(payload):
            sent.append(payload)
    fake.Emails = _Emails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)

    monkeypatch.setattr(oa, "_cooldown_acquired", lambda *_a: True)
    assert send_ops_alert("watchdog", "job-1", "Job died", "job_id=job-1") is True
    assert sent and "[BridgeLeads OPS] Job died" == sent[0]["subject"]
    assert "job_id=job-1" in sent[0]["html"]

    sent.clear()
    monkeypatch.setattr(oa, "_cooldown_acquired", lambda *_a: False)
    assert send_ops_alert("watchdog", "job-1", "Job died", "job_id=job-1") is False
    assert sent == []  # suppressed — nothing sent


# ─── P0-09: the second, independent route (Better Stack incoming webhook) ────────────
# A REAL HTTP server on 127.0.0.1 receives the POST, so the wire payload, the status
# handling and the log hygiene are all exercised for real. Only Resend (an external API)
# is faked, as above.

_WEBHOOK_PATH = "/integration/secret-token-p009"


@pytest.fixture
def webhook_server():
    """Yields (url, received, set_status). ``received`` collects decoded JSON bodies."""
    received: list[dict] = []
    state = {"status": 200}  # a 3xx also sends Location, to prove it is not followed

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — http.server API
            n = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(n)))
            self.send_response(state["status"])
            if 300 <= state["status"] < 400:
                self.send_header("Location", "/login")
            self.end_headers()

        def do_GET(self):  # noqa: N802 — a followed redirect would arrive here
            received.append({"GET": self.path})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield (
            f"http://127.0.0.1:{srv.server_port}{_WEBHOOK_PATH}",
            received,
            lambda code: state.__setitem__("status", code),
        )
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def details(monkeypatch):
    """Capture the audit row's detail for each alert."""
    out: list[str] = []

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def add(self, obj):
            out.append(obj.detail)

        def commit(self):
            pass

    monkeypatch.setattr(ops_alerts, "_session_for_persist", lambda: _S())
    return out


def _fake_resend(monkeypatch, *, fail: bool):
    import sys
    import types

    def _send(_payload):
        if fail:
            raise RuntimeError("resend exploded")

    fake = types.ModuleType("resend")
    fake.api_key = None
    fake.Emails = type("E", (), {"send": staticmethod(_send)})
    monkeypatch.setitem(sys.modules, "resend", fake)


def test_webhook_route_posts_the_better_stack_payload(monkeypatch, webhook_server, details):
    url, received, _ = webhook_server
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", url)
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda *_a: True)

    assert send_ops_alert("canary", "pierce/wa/divorce", "Canary ZERO OUTPUT", "was healthy") is True

    assert received == [{
        "status": "alert",
        "alert_id": "canary:pierce/wa/divorce",
        "title": "[BridgeLeads OPS] Canary ZERO OUTPUT",
        "description": "was healthy",
        "kind": "canary",
        "key": "pierce/wa/divorce",
    }]
    assert details == ["[sent] [email:off webhook:ok] Canary ZERO OUTPUT"]


def test_webhook_rejection_never_raises_and_never_logs_the_url(
    monkeypatch, webhook_server, details, caplog
):
    """The URL is the credential. A 500 must come back as False, be recorded as a
    failure, and leave the URL out of every log line at ANY level, DEBUG included
    (Codex P1: urllib3 logs request paths at DEBUG, and the token is the path)."""
    url, received, set_status = webhook_server
    set_status(500)
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", url)
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda *_a: True)

    with caplog.at_level(logging.DEBUG):
        assert send_ops_alert("billing", "gap/x", "Billing gap", "b") is False

    assert len(received) == 1
    assert details == ["[undelivered] [email:off webhook:fail] Billing gap"]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "webhook failed" in logged and "status=500" in logged
    assert "secret-token-p009" not in logged and "127.0.0.1" not in logged


def test_unreachable_webhook_never_raises(monkeypatch, details):
    import socket

    with socket.socket() as s:  # a port nothing listens on once closed
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", f"http://127.0.0.1:{port}{_WEBHOOK_PATH}")
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda *_a: True)

    assert send_ops_alert("canary", "x/WA", "s", "b") is False
    assert details == ["[undelivered] [email:off webhook:fail] s"]


def test_an_email_failure_does_not_stop_the_webhook(monkeypatch, webhook_server, details):
    """The whole point of a second route: Resend breaking must not silence Better Stack."""
    url, received, _ = webhook_server
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.bridgeleads.io")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", url)
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda *_a: True)
    _fake_resend(monkeypatch, fail=True)

    assert send_ops_alert("watchdog", "job-1", "Job died", "job_id=job-1") is True
    assert len(received) == 1
    assert details == ["[sent] [email:fail webhook:ok] Job died"]


def test_each_route_has_its_own_cooldown(monkeypatch, webhook_server, details):
    """An e-mail cooldown (e.g. one set before the webhook existed) must not mute the
    webhook, and the webhook's bucket must not mute e-mail (Codex P1)."""
    url, received, _ = webhook_server
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "ops@test.bridgeleads.io")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_not_real")
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", url)
    _fake_resend(monkeypatch, fail=False)

    asked: list[tuple] = []

    def _cooldown(kind, key, route="email"):
        asked.append((kind, key, route))
        return route == "webhook"  # e-mail bucket held, webhook bucket free

    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", _cooldown)

    assert send_ops_alert("canary", "x/WA", "s", "b") is True
    assert asked == [("canary", "x/WA", "email"), ("canary", "x/WA", "webhook")]
    assert len(received) == 1
    assert details == ["[sent] [email:cooldown webhook:ok] s"]


def test_route_cooldown_buckets_are_separate_in_redis():
    """Against the real Redis: holding the e-mail bucket must leave the webhook bucket
    free, and the e-mail bucket keeps its pre-P0-09 key so cooldowns already running in
    production survive the deploy."""
    import uuid

    import redis as sync_redis

    kind, key = f"p009test-{uuid.uuid4().hex}", "x/WA"
    r = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    try:
        assert ops_alerts._cooldown_acquired(kind, key) is True  # e-mail, default route
        assert ops_alerts._cooldown_acquired(kind, key) is False  # now held
        assert ops_alerts._cooldown_acquired(kind, key, "webhook") is True  # still free
        assert ops_alerts._cooldown_acquired(kind, key, "webhook") is False
        assert r.exists(f"ops_alert:{kind}:{key}") == 1
        assert r.exists(f"ops_alert:webhook:{kind}:{key}") == 1
    finally:
        r.delete(f"ops_alert:{kind}:{key}", f"ops_alert:webhook:{kind}:{key}")
        r.close()


def test_a_redirect_is_not_delivery(monkeypatch, webhook_server, details):
    """A 302 (say, to a login page) must count as a failure, and must not be followed:
    the server sees exactly one request (Codex P2)."""
    url, received, set_status = webhook_server
    set_status(302)
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", url)
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda *_a: True)

    assert send_ops_alert("canary", "x/WA", "s", "b") is False
    assert len(received) == 1
    assert details == ["[undelivered] [email:off webhook:fail] s"]


def test_a_failed_send_frees_its_cooldown_for_the_next_occurrence(monkeypatch, details):
    """Against the real Redis: a webhook failure must not mute that route for the whole
    cooldown, so the next occurrence retries (Codex P2)."""
    import socket
    import uuid

    import redis as sync_redis

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", f"http://127.0.0.1:{port}{_WEBHOOK_PATH}")
    kind, key = f"p009test-{uuid.uuid4().hex}", "x/WA"
    r = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    try:
        assert send_ops_alert(kind, key, "s", "b") is False
        assert r.exists(f"ops_alert:webhook:{kind}:{key}") == 0
        assert send_ops_alert(kind, key, "s", "b") is False
        assert details == ["[undelivered] [email:off webhook:fail] s"] * 2  # retried, not cooldown
    finally:
        r.delete(f"ops_alert:webhook:{kind}:{key}")
        r.close()


def test_a_non_http_webhook_url_is_refused(monkeypatch, tmp_path, details, caplog):
    """file:// would let urllib read a local file; only http(s) is ever opened. The file
    exists, so the refusal must come from the scheme check, not a missing path."""
    target = tmp_path / "x"
    target.write_text("{}")
    monkeypatch.setattr(settings, "OPS_ALERT_WEBHOOK_URL", target.as_uri())
    monkeypatch.setattr(ops_alerts, "_cooldown_acquired", lambda *_a: True)

    with caplog.at_level(logging.ERROR, logger="worker.ops_alerts"):
        assert send_ops_alert("canary", "x/WA", "s", "b") is False
    assert details == ["[undelivered] [email:off webhook:fail] s"]
    assert any("ValueError" in r.getMessage() for r in caplog.records)
