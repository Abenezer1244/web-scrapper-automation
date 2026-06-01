"""Tests for the SSRF-safe outbound HTTP helper (security review HIGH-3/HIGH-4 foundation).

No network: every assertion exercises a path that raises BEFORE requests.get
(literal IPs need no DNS; cross-origin is rejected pre-fetch).
"""
import pytest

from src.utils.safe_http import safe_get, safe_get_following, same_origin


@pytest.mark.parametrize(
    "url,ref,expected",
    [
        ("https://portal.county.gov/a", "https://portal.county.gov/b", True),
        ("https://portal.county.gov:443/a", "https://portal.county.gov/b", True),  # default port
        ("https://PORTAL.County.gov./a", "https://portal.county.gov/b", True),     # case + trailing dot
        ("https://evil.county.gov/a", "https://portal.county.gov/b", False),       # sibling host
        ("http://portal.county.gov/a", "https://portal.county.gov/b", False),      # scheme
        ("https://portal.county.gov:8443/a", "https://portal.county.gov/b", False),  # port
    ],
)
def test_same_origin(url, ref, expected):
    assert same_origin(url, ref) is expected


def test_safe_get_blocks_metadata_ip():
    with pytest.raises(ValueError):
        safe_get("https://169.254.169.254/latest/meta-data/")


def test_safe_get_blocks_private_ip():
    with pytest.raises(ValueError):
        safe_get("https://10.0.0.5/internal")


def test_safe_get_blocks_loopback_hostname():
    with pytest.raises(ValueError):
        safe_get("https://localhost/x")


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/obj",  # metadata IP (initial hop)
        "https://10.0.0.1/obj",          # private IP
        "https://localhost/obj",         # loopback hostname
    ],
)
def test_safe_get_following_blocks_initial_hop(url):
    # The redirect-following variant must still reject a blocked initial URL
    # before any request (per-hop validation reuses the same check).
    with pytest.raises(ValueError):
        safe_get_following(url)


def test_safe_get_refuses_cross_origin_when_pinned():
    # Literal public IPs (no DNS): validation passes, origin pin rejects the
    # cross-origin fetch before any cookies could be sent.
    with pytest.raises(ValueError, match="different origin"):
        safe_get(
            "https://93.184.216.34/detail",
            same_origin_as="https://8.8.8.8/",
            cookies={"session": "secret"},
        )
