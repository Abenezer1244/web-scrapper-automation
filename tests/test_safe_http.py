"""Tests for the SSRF-safe outbound HTTP helper (security review HIGH-3/HIGH-4 foundation).

No network: every assertion exercises a path that raises BEFORE requests.get
(literal IPs need no DNS; cross-origin is rejected pre-fetch).
"""
import pytest

from src.utils.safe_http import (
    _stream_capped,
    safe_download_to_file,
    safe_get,
    safe_get_following,
    same_origin,
)


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


# ─── safe_download_to_file: SSRF / arg guards (raise BEFORE any network) ──────

@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/file",  # metadata IP
        "https://10.0.0.1/file",          # private IP
        "https://localhost/file",         # loopback hostname
    ],
)
def test_safe_download_blocks_bad_initial_hop(url, tmp_path):
    # Per-hop SSRF validation must reject a blocked initial URL before streaming.
    with pytest.raises(ValueError):
        safe_download_to_file(url, str(tmp_path / "out.bin"), max_bytes=1024)
    assert not (tmp_path / "out.bin").exists()  # nothing written


def test_safe_download_rejects_scheme_downgrade(tmp_path):
    # require_https default True: a plaintext hop is refused before any fetch
    # (literal public IP, so the raise is the HTTPS check, not DNS/SSRF).
    with pytest.raises(ValueError, match="HTTPS required"):
        safe_download_to_file(
            "http://93.184.216.34/file", str(tmp_path / "out.bin"), max_bytes=1024
        )


def test_safe_download_rejects_nonpositive_cap(tmp_path):
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        safe_download_to_file(
            "https://93.184.216.34/file", str(tmp_path / "out.bin"), max_bytes=0
        )


# ─── _stream_capped: size-cap + empty behaviour (pure, real temp-file I/O) ────

def test_stream_capped_writes_all_and_returns_count(tmp_path):
    dest = tmp_path / "out.bin"
    total = _stream_capped([b"abc", b"defgh", b"i"], str(dest), max_bytes=100)
    assert total == 9
    assert dest.read_bytes() == b"abcdefghi"


def test_stream_capped_skips_empty_chunks(tmp_path):
    dest = tmp_path / "out.bin"
    total = _stream_capped([b"", b"ab", b"", b"c"], str(dest), max_bytes=100)
    assert total == 3
    assert dest.read_bytes() == b"abc"


def test_stream_capped_aborts_over_cap(tmp_path):
    dest = tmp_path / "out.bin"
    # Cap is 5; the third chunk pushes the running total to 6 → abort.
    with pytest.raises(ValueError, match="size cap"):
        _stream_capped([b"aa", b"bb", b"cc"], str(dest), max_bytes=5)


def test_stream_capped_empty_iterable_returns_zero(tmp_path):
    dest = tmp_path / "out.bin"
    assert _stream_capped([], str(dest), max_bytes=100) == 0
    assert dest.read_bytes() == b""
