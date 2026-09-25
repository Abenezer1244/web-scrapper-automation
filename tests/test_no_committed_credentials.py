"""No tracked file may carry a credential-shaped assignment (audit 2026-09-25, N-01).

A live Cloudflare API token sat in ``infra/terraform/terraform.tfvars`` from
2026-03-17 until this test existed. Two audits called history "clean" because
their patterns were upper-case vendor prefixes anchored at line start, and the
file opened with a UTF-8 BOM. So the scanner here is case-insensitive, strips a
BOM, keys on the assignment's NAME rather than a vendor prefix, and proves on a
positive control that it still finds a BOM-prefixed token.

It scans ``git ls-files``, i.e. what a push would publish, not the working tree.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_ASSIGNMENT = re.compile(
    r"(?im)^[﻿\s\"'-]*"
    r"([a-z0-9_]*(?:api[_-]?token|api[_-]?key|secret(?:[_-]?key)?|password|private[_-]?key|access[_-]?key)[a-z0-9_]*)"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/]{24,})"
)

# A value counts as a placeholder only if it says so. Anything else that looks
# like a credential fails the test: add the real fix (env var), not an entry here.
_PLACEHOLDER_MARKERS = ("your", "change", "example", "placeholder", "fake", "dummy", "test", "ci-", "timing")

# Names that are constants holding a header name, not a secret value.
_NON_SECRET_NAMES = {"_SECRET_HEADER"}

# (path, name) pairs reviewed by hand as local test fixtures. Keep this tiny:
# each entry is a decision that the value signs nothing outside the test process.
_REVIEWED_TEST_FIXTURES = {
    # Signs synthetic Stripe events inside the test only; never a Stripe secret.
    ("tests/test_promo_access.py", "_WEBHOOK_SECRET"),
}

_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip", ".lock"}


def find_credential_assignments(text: str) -> list[tuple[str, str]]:
    """(name, redacted value) for every non-placeholder credential assignment."""
    found = []
    for m in _ASSIGNMENT.finditer(text):
        name, value = m.group(1), m.group(2)
        if name in _NON_SECRET_NAMES:
            continue
        if any(marker in value.lower() for marker in _PLACEHOLDER_MARKERS):
            continue
        found.append((name, f"{value[:4]}...{value[-3:]}"))
    return found


def test_scanner_finds_a_bom_prefixed_lowercase_token():
    # Positive control: the exact shape that slipped past two audits.
    sample = '﻿cloudflare_api_token = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"\n'
    assert find_credential_assignments(sample) == [("cloudflare_api_token", "AbCd...bcd")]


def test_scanner_ignores_declared_placeholders():
    sample = 'TRACERFY_API_TOKEN=your-tracerfy-token-goes-here\nSECRET_KEY=ci-test-secret-key-that-is-long-enough\n'
    assert find_credential_assignments(sample) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_no_tracked_file_carries_a_credential():
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout.decode("utf-8").split("\0")
    offenders = []
    for rel in filter(None, listed):
        path = REPO / rel
        if path.suffix.lower() in _BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders += [
            (rel, name, redacted)
            for name, redacted in find_credential_assignments(text)
            if (rel, name) not in _REVIEWED_TEST_FIXTURES
        ]
    assert offenders == [], f"credential-shaped values in tracked files: {offenders}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_terraform_tfvars_is_not_tracked():
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "infra/terraform/terraform.tfvars"],
        cwd=REPO, capture_output=True,
    )
    assert tracked.returncode != 0, "infra/terraform/terraform.tfvars is tracked again"
