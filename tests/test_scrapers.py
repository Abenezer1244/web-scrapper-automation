"""Tests for scraper security, registry, hashing, and CSV sanitization."""
import pytest

from src.api.middleware.security import sanitize_for_csv, validate_scraping_target
from src.scrapers.base_scraper import BridgeScraper
from src.scrapers.registry import UnsupportedCountyError, get_scraper_class

# ─── validate_scraping_target — SSRF prevention ───────────────────────────────

@pytest.mark.parametrize("url", [
    "http://armsweb.co.pierce.wa.us/SearchEntry.aspx",      # HTTP (not HTTPS)
    "https://evil.com/steal",                                 # Unknown domain
    "https://192.168.1.1/admin",                             # RFC1918 private
    "https://10.0.0.1/",                                     # RFC1918 private
    "https://172.16.0.1/",                                   # RFC1918 private
    "https://127.0.0.1/",                                    # Loopback
    "https://169.254.169.254/latest/meta-data/",             # AWS metadata
    "https://metadata.google.internal/",                     # GCP metadata
    "https://[::1]/",                                        # IPv6 loopback
])
def test_validate_scraping_target_blocks_unsafe_urls(url: str):
    with pytest.raises(ValueError):
        validate_scraping_target(url)


@pytest.mark.parametrize("url", [
    "https://armsweb.co.pierce.wa.us/SearchEntry.aspx",
    "https://atip.piercecountywa.gov/app/v2/parcelSearch/search",
])
def test_validate_scraping_target_allows_known_domains(url: str):
    # Must not raise
    validate_scraping_target(url)


# ─── sanitize_for_csv — injection prevention ──────────────────────────────────

@pytest.mark.parametrize("injection,expected_prefix", [
    ("=SUM(A1)", "'"),
    ("+HYPERLINK(\"x\")", "'"),
    ("-1+1", "'"),
    ("@username", "'"),
    ("\t=cmd", "'"),
    ("\r=cmd", "'"),
])
def test_sanitize_for_csv_prefixes_dangerous_chars(injection, expected_prefix):
    result = sanitize_for_csv(injection)
    assert result.startswith(expected_prefix)


@pytest.mark.parametrize("safe_value", [
    "Smith, John",
    "123 Main St",
    "LOT 4 BLOCK 2 SEC 12",
    "1234567890",
    "",
    "01/15/2024",
])
def test_sanitize_for_csv_leaves_safe_values_unchanged(safe_value):
    assert sanitize_for_csv(safe_value) == safe_value


# ─── BridgeScraper.make_hash — deduplication ─────────────────────────────────

def test_make_hash_identical_rows_produce_same_hash():
    row = {"party_name": "Smith", "parcel_id": "1234567890", "date_recorded": "01/15/2024"}
    h1 = BridgeScraper.make_hash(row)
    h2 = BridgeScraper.make_hash(row)
    assert h1 == h2


def test_make_hash_different_rows_produce_different_hashes():
    row_a = {"party_name": "Smith", "parcel_id": "1234567890"}
    row_b = {"party_name": "Jones", "parcel_id": "0987654321"}
    assert BridgeScraper.make_hash(row_a) != BridgeScraper.make_hash(row_b)


def test_make_hash_returns_32_char_hex_string():
    row = {"party_name": "Smith", "parcel_id": "1234567890"}
    h = BridgeScraper.make_hash(row)
    assert isinstance(h, str)
    assert len(h) == 32
    assert all(c in "0123456789abcdef" for c in h)


def test_make_hash_key_order_invariant():
    """Dict key order must not affect the hash."""
    row_a = {"parcel_id": "1234567890", "party_name": "Smith"}
    row_b = {"party_name": "Smith", "parcel_id": "1234567890"}
    assert BridgeScraper.make_hash(row_a) == BridgeScraper.make_hash(row_b)


def test_deduplication_removes_duplicate_hashes():
    rows = [
        {"party_name": "Smith", "parcel_id": "1111111111"},
        {"party_name": "Smith", "parcel_id": "1111111111"},  # duplicate
        {"party_name": "Jones", "parcel_id": "2222222222"},
    ]
    seen = set()
    unique = []
    for row in rows:
        h = BridgeScraper.make_hash(row)
        if h not in seen:
            seen.add(h)
            unique.append(row)
    assert len(unique) == 2


# ─── Registry ─────────────────────────────────────────────────────────────────

def test_registry_lookup_pierce_wa_probate():
    """The seed connector (pierce/WA/probate) must be resolvable."""
    cls = get_scraper_class("pierce", "WA", "probate")
    assert cls is not None


def test_registry_lookup_case_insensitive():
    """County and state lookups must be case-insensitive."""
    cls_lower = get_scraper_class("pierce", "wa", "probate")
    cls_upper = get_scraper_class("PIERCE", "WA", "PROBATE")
    assert cls_lower is cls_upper


def test_registry_unsupported_county_raises():
    with pytest.raises(UnsupportedCountyError):
        get_scraper_class("atlantis", "XX", "probate")


def test_registry_unsupported_record_type_raises():
    with pytest.raises(UnsupportedCountyError):
        get_scraper_class("pierce", "WA", "fake_record_type")
