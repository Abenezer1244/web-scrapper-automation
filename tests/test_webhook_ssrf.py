"""SSRF guard tests for outbound webhooks (security review CRITICAL-1 / HIGH-1).

No mocks: literal-IP and scheme checks need no network; the DNS-rebinding
path is exercised deterministically via `localhost` (always resolves to a
loopback address locally) and the reserved `.invalid` TLD (RFC 6761 —
guaranteed to never resolve), so these tests are network-independent.
"""
import ipaddress

import pytest

from src.api.middleware.security import (
    _assert_resolved_ips_safe,
    _ip_is_blocked,
    validate_outbound_webhook,
)


def test_webhook_requires_https():
    with pytest.raises(ValueError, match="HTTPS"):
        validate_outbound_webhook("http://example.com/hook")


def test_webhook_rejects_overlong_url():
    with pytest.raises(ValueError, match="too long"):
        validate_outbound_webhook("https://example.com/" + "a" * 2100)


def test_webhook_rejects_missing_host():
    with pytest.raises(ValueError):
        validate_outbound_webhook("https:///nohost")


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1/",                          # loopback
        "https://10.0.0.5/internal",                   # RFC1918
        "https://192.168.1.1/",                        # RFC1918
        "https://100.64.0.1/",                         # CGNAT
        "https://[::1]/",                              # IPv6 loopback
        "https://[::ffff:169.254.169.254]/",           # IPv4-mapped metadata
    ],
)
def test_webhook_blocks_literal_private_and_metadata_ips(url):
    with pytest.raises(ValueError):
        validate_outbound_webhook(url)


def test_webhook_blocks_metadata_hostname():
    with pytest.raises(ValueError):
        validate_outbound_webhook("https://metadata.google.internal/")


@pytest.mark.parametrize(
    "ip,blocked",
    [
        ("169.254.169.254", True),
        ("10.1.2.3", True),
        ("100.64.0.1", True),         # CGNAT
        ("0.0.0.0", True),
        ("255.255.255.255", True),
        ("::ffff:10.0.0.1", True),    # IPv4-mapped IPv6 must map back to v4
        ("8.8.8.8", False),           # public
        ("93.184.216.34", False),     # public (example.com range)
    ],
)
def test_ip_is_blocked_table(ip, blocked):
    assert _ip_is_blocked(ipaddress.ip_address(ip)) is blocked


def test_resolution_blocks_loopback_host():
    # `localhost` resolves to a loopback address; the resolved-IP check
    # must reject it (this is the DNS-rebinding defense in action).
    with pytest.raises(ValueError, match="blocked address"):
        _assert_resolved_ips_safe("localhost")


def test_resolution_fails_closed_on_unresolvable_host():
    # Reserved .invalid TLD never resolves -> must fail closed, not allow.
    with pytest.raises(ValueError, match="could not be resolved"):
        _assert_resolved_ips_safe("definitely-not-real.invalid")


def test_worker_blocks_ssrf_webhook_without_posting():
    # Run the real Celery task eagerly (no broker, no network): a metadata-IP
    # webhook target must short-circuit to status="blocked" before any POST,
    # and must NOT raise (which would trigger a Celery retry).
    from src.workers.webhook_delivery import deliver_job_webhook

    result = deliver_job_webhook.apply(
        args=("job-1234", "https://169.254.169.254/steal", {"event": "job.completed"})
    )
    assert result.successful()
    assert result.get()["status"] == "blocked"
