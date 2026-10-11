"""P0-04b: PII and bearer links travel to Celery tasks sealed, never as plaintext.

Release A (consumers): the delivery tasks accept a ``sealed`` envelope beside the
legacy plaintext arguments, so a producer can switch over once every worker runs
this code. The webhook test POSTs to a real local HTTP server; the email tests
stop at Resend's send call (external API, as in test_workers.py).
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet, InvalidToken

from src.utils.crypto import seal_task_args, unseal_task_args
from src.workers import webhook_delivery as wd
from tests.test_webhook_egress_hardening import loopback_transport  # noqa: F401  (fixture)

_LEAD = {"party_name": "Jane Q Marker", "phone": "+12535550100", "email": "jane@marker.example",
         "property_address": "1234 Marker Elm St, Tacoma, WA 98402",
         "mailing_address": "PO Box 9876, Markerville, WA 98001"}


# ── the envelope ─────────────────────────────────────────────────────────────

def test_round_trip_and_nothing_readable_in_the_token():
    args = {"recipient_emails": ["jane@marker.example"], "download_url": "https://x/d?token=abc"}
    token = seal_task_args(args)
    assert token.startswith("tk1:")
    assert "marker" not in token.lower() and "token=abc" not in token
    assert unseal_task_args(token, required=("recipient_emails", "download_url")) == args


@pytest.mark.parametrize("token, error", [
    ("tk1:" + Fernet(Fernet.generate_key()).encrypt(b'{"email":"x"}').decode(), InvalidToken),  # other key
    ("not-sealed", ValueError),                                                                 # plaintext
    (None, ValueError),
])
def test_unseal_is_strict(token, error):
    with pytest.raises(error):
        unseal_task_args(token, required=("email",))


def test_a_tampered_token_does_not_open():
    token = seal_task_args({"email": "jane@marker.example"})
    flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    with pytest.raises(InvalidToken):
        unseal_task_args(flipped, required=("email",))


def test_missing_required_key_raises():
    with pytest.raises(ValueError, match="missing"):
        unseal_task_args(seal_task_args({"url": "https://x"}), required=("url", "payload"))


def test_a_500_lead_dialer_payload_stays_small():
    # Worst case the generic dialer pushes: 500 leads in one request.
    token = seal_task_args({"url": "https://hooks.example.com/in", "payload": {"leads": [_LEAD] * 500}})
    assert len(token) < 300_000  # ~1.4x compact JSON; far below Redis's 512 MB value cap


# ── deliver_job_email ────────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    import resend

    from src.config import settings

    calls: list[dict] = []
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(resend.Emails, "send", lambda params: calls.append(params) or {"id": "x"})
    return calls


_EMAIL_ARGS = {"scraper_name": "Pierce County Probate", "record_count": 10,
               "download_url": "https://api.example/d/abc", "recipient_emails": ["jane@marker.example"],
               "fmt": "csv"}


def test_sealed_and_legacy_email_send_the_identical_message(sent):
    from src.workers import delivery

    job_id = str(uuid.uuid4())
    assert delivery.deliver_job_email.apply(kwargs={"job_id": job_id, **_EMAIL_ARGS}).successful()
    assert delivery.deliver_job_email.apply(
        kwargs={"job_id": job_id, "sealed": seal_task_args(_EMAIL_ARGS)}).successful()
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[0]["to"] == ["jane@marker.example"]


def test_a_sealed_email_that_does_not_open_fails_and_sends_nothing(sent):
    from src.workers import delivery

    bad = "tk1:" + Fernet(Fernet.generate_key()).encrypt(json.dumps(_EMAIL_ARGS).encode()).decode()
    result = delivery.deliver_job_email.apply(kwargs={"job_id": str(uuid.uuid4()), "sealed": bad})
    assert result.failed() and sent == []


def test_a_retry_never_carries_resends_message(monkeypatch):
    import resend
    from celery.exceptions import Retry
    from resend import exceptions as resend_ex

    from src.config import settings
    from src.workers import delivery

    def _rate_limited(*_a, **_k):
        raise resend_ex.ApplicationError(message="limit for jane@marker.example",
                                         error_type="rate_limit_exceeded", code=429)

    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(resend.Emails, "send", _rate_limited)
    # Called directly, self.retry(exc=...) raises the very exception Celery would
    # store in the result backend for a queued retry.
    with pytest.raises((RuntimeError, Retry)) as caught:
        delivery.deliver_job_email.run(job_id=str(uuid.uuid4()), sealed=seal_task_args(_EMAIL_ARGS))
    import traceback

    assert "ApplicationError (code=429)" in str(caught.value)
    # Not in the message, nor chained in as context, nor anywhere in the traceback
    # Celery would store with the retry.
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert "marker" not in rendered.lower()


# ── send_duplicate_signup_email ──────────────────────────────────────────────

def test_sealed_and_legacy_duplicate_signup_send_the_identical_message(sent):
    from src.workers import onboarding_emails

    onboarding_emails.send_duplicate_signup_email.apply(args=("jane@marker.example",))
    onboarding_emails.send_duplicate_signup_email.apply(
        kwargs={"sealed": seal_task_args({"email": "jane@marker.example"})})
    assert len(sent) == 2 and sent[0] == sent[1]


# ── deliver_job_webhook (real local HTTP server) ─────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def do_POST(self):  # noqa: N802 — http.server API
        _Recorder.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def recorder(loopback_transport):  # noqa: F811
    _Recorder.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/in?token=SECRET123"
    server.shutdown()


def test_sealed_and_legacy_webhook_post_the_identical_body(recorder):
    payload = {"event": "leads.dialer_ready", "leads": [_LEAD], "signature": "abc"}
    legacy = wd.deliver_job_webhook.apply(args=("job-1", recorder, payload)).get()
    sealed = wd.deliver_job_webhook.apply(
        args=("job-1",), kwargs={"sealed": seal_task_args({"url": recorder, "payload": payload})}).get()
    assert legacy["status"] == sealed["status"] == "delivered"
    assert len(_Recorder.bodies) == 2 and _Recorder.bodies[0] == _Recorder.bodies[1]


def test_a_sealed_webhook_that_does_not_open_posts_nothing(recorder):
    result = wd.deliver_job_webhook.apply(args=("job-1",), kwargs={"sealed": "tk1:garbage"})
    assert result.failed() and _Recorder.bodies == []


# ── ingest_tracerfy_batch ────────────────────────────────────────────────────

def test_tracerfy_ingest_no_longer_requires_the_download_url():
    import inspect

    from src.workers.tracerfy_ingest import ingest_tracerfy_batch

    params = inspect.signature(ingest_tracerfy_batch.run).parameters
    assert params["download_url"].default == ""
