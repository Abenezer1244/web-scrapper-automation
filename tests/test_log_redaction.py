"""Central log-redaction filter tests (security review Low: no central redaction).

Defense-in-depth: a stray log call must not leak a credential to console/file/Loki.
"""
import logging

import pytest

from src.utils.logger import _redaction_filter


def _redact(msg: str, *args) -> str:
    record = logging.LogRecord("t", logging.INFO, "f", 1, msg, args, None)
    _redaction_filter.filter(record)
    return record.getMessage()


@pytest.mark.parametrize(
    "msg",
    [
        "Authorization: Bearer abc123.def456-tok",
        'login body {"password": "hunter2secret"}',
        "config api_key=sk-abcdef1234567890abcdef",
        "x-api-key: deadbeefdeadbeef1234",
        "download token=eyJhdr.payloadpart123.sigpart45678",
        "jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sigpart_abc123",
        "stripe key sk-live-abcdef1234567890ABCDEF",
        "Set-Cookie: sb-access-token=eyJhdr.body.sig; HttpOnly; Secure",
        "Cookie: session=abc123; csrf=def456",
        "fetching https://admin:s3cretpass@internal.host/path",
    ],
)
def test_secrets_are_redacted(msg):
    out = _redact(msg)
    assert "REDACTED" in out, f"not redacted: {out}"


@pytest.mark.parametrize(
    "msg",
    [
        "record saved for 123 Main St, Tacoma WA",
        "Scrape complete — 152 new leads (8 duplicates)",
        "Navigated to https://recordsearch.kingcounty.gov/search",
    ],
)
def test_normal_messages_unchanged(msg):
    assert _redact(msg) == msg
