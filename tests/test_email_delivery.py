"""Unit tests for the retryable lead-delivery email (Fix 3).

Covers the pure pieces — the transient-vs-permanent error classifier and the
email builder — without sending anything. Real Resend is an external API, so the
classifier is exercised with constructed SDK errors (no network).
"""
import requests
from resend import exceptions as resend_ex

from src.workers.delivery import (
    _build_lead_delivery_email,
    _is_retryable_email_error,
)


def _base_error(code):
    """Construct a base ResendError with the given HTTP `code`."""
    return resend_ex.ResendError(code=code, error_type="x", message="boom", suggested_action="y")


def _subclass_error(cls, code):
    """Construct a Resend SDK error SUBCLASS (different init from the base)."""
    return cls(message="boom", error_type="x", code=code)


def test_network_errors_are_retryable():
    assert _is_retryable_email_error(requests.ConnectionError("dns")) is True
    assert _is_retryable_email_error(requests.Timeout("slow")) is True


def test_transient_status_codes_are_retryable():
    for code in (408, 409, 429, 500, 502, 503):
        assert _is_retryable_email_error(_base_error(code)) is True, code


def test_permanent_status_codes_not_retryable():
    for code in (400, 401, 403, 404, 422):
        assert _is_retryable_email_error(_base_error(code)) is False, code


def test_named_permanent_errors_not_retryable():
    # Auth/validation failures fail identically on every attempt — never retry,
    # even if their status code were 5xx-shaped.
    assert _is_retryable_email_error(_subclass_error(resend_ex.ValidationError, 422)) is False
    assert _is_retryable_email_error(_subclass_error(resend_ex.InvalidApiKeyError, 401)) is False


def test_client_side_value_error_not_retryable():
    # A missing-argument ValueError (Resend raises these client-side) is a code
    # bug, not a transient failure — retrying would just loop.
    assert _is_retryable_email_error(ValueError("missing field")) is False


def test_build_email_has_subject_link_and_format():
    subject, html_body, text_body = _build_lead_delivery_email(
        "Pierce County Probate", 1234, "https://app.example.com/jobs/x/download?token=abc", "excel"
    )
    assert "Pierce County Probate" in subject
    assert "1,234" in subject  # thousands-formatted record count
    # Link present in both parts; format surfaced as the button/label.
    assert "https://app.example.com/jobs/x/download?token=abc" in text_body
    assert "EXCEL" in html_body
    assert "expires in 48 hours" in text_body
