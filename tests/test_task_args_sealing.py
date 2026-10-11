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


# ── release B: producers send only an id + sealed ────────────────────────────

_SEALED_TASKS = {"deliver_job_email", "deliver_job_webhook", "send_duplicate_signup_email",
                 "ingest_tracerfy_batch"}
_ALLOWED_KWARGS = {"job_id", "queue_id", "sealed", "kwargs", "countdown"}
_ALLOWED_INNER_KWARGS = {"queue_id", "provider_rechecks", "job_id", "sealed"}


def _plaintext_enqueues(source: str, filename: str) -> list[str]:
    """Every .delay / .apply_async of a sealed task that passes more than an id
    (one positional, or job_id / queue_id) plus `sealed`."""
    import ast

    bad = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("delay", "apply_async")
                and isinstance(node.func.value, ast.Name) and node.func.value.id in _SEALED_TASKS):
            continue
        where = f"{filename}:{node.lineno}"
        # Only the webhook task takes a positional, and it is the job id; any
        # other positional is a plaintext value (e.g. the duplicate-signup email).
        if len(node.args) > (1 if node.func.value.id == "deliver_job_webhook" else 0):
            bad.append(f"{where} positional args")
        for kw in node.keywords:
            if kw.arg not in _ALLOWED_KWARGS:
                bad.append(f"{where} {kw.arg}=")
            if kw.arg == "kwargs" and isinstance(kw.value, ast.Dict):
                bad += [f"{where} kwargs[{k.value}]" for k in kw.value.keys
                        if isinstance(k, ast.Constant) and k.value not in _ALLOWED_INNER_KWARGS]
    return bad


def test_no_producer_enqueues_plaintext_arguments():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    bad = [b for f in src.rglob("*.py")
           for b in _plaintext_enqueues(f.read_text(encoding="utf-8"), str(f.relative_to(src)))]
    assert bad == []


def test_the_policy_check_flags_the_old_calls():
    old = (
        "deliver_job_email.delay(job_id=j, scraper_name=n, recipient_emails=e)\n"
        "deliver_job_webhook.delay(j, url, payload)\n"
        "send_duplicate_signup_email.delay(email)\n"
        "ingest_tracerfy_batch.delay(queue_id=q, download_url=u)\n"
        "ingest_tracerfy_batch.apply_async(kwargs={'queue_id': q, 'download_url': ''})\n"
    )
    assert len(_plaintext_enqueues(old, "old.py")) == 6


async def test_the_real_duplicate_signup_message_carries_no_address():
    # Published through Celery to the test Redis broker; before_task_publish
    # sees exactly what the broker stores (body + headers, incl. kwargsrepr,
    # which is what a worker's "Received task" line prints).
    from celery.signals import before_task_publish

    from src.api.routes.auth_helpers.registration import _notify_existing_account

    published: list[tuple] = []

    def _capture(sender=None, body=None, headers=None, **_kw):
        published.append((sender, body, headers))

    before_task_publish.connect(_capture, weak=False)
    try:
        await _notify_existing_account(f"jane+{uuid.uuid4().hex[:8]}@marker.example")
    finally:
        before_task_publish.disconnect(_capture)
    (sender, body, headers), = [p for p in published if p[0].endswith("send_duplicate_signup_email")]
    wire = json.dumps([body, headers], default=str).lower()
    assert "marker" not in wire and "jane" not in wire
    sealed = body[1]["sealed"]
    assert unseal_task_args(sealed, required=("email",))["email"].endswith("@marker.example")
