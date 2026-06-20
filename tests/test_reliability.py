"""Tests for src/scrapers/reliability.py — fail-loud scraper primitives.

Pure string/logic tests, no mocks.
"""
import pytest

from src.scrapers.reliability import (
    ScraperBlockedError,
    ScraperExecutionError,
    check_extraction_canary,
    classify_results_page,
    detect_block,
)

# ── detect_block ──────────────────────────────────────────────────────────────

BLOCK_TEXTS = [
    "Please complete the reCAPTCHA to continue",
    "hCaptcha verification required",
    "Attention Required! | Cloudflare",
    "Checking your browser before accessing the site",
    "cf-ray: 8a1b2c3d",
    "Please verify you are human",
    "Are you a human?",
    "We have detected unusual traffic from your network",
    "429 Too Many Requests",
    "rate limit exceeded, slow down",
]


@pytest.mark.parametrize("text", BLOCK_TEXTS)
def test_detect_block_positives(text):
    assert detect_block(text) is not None


NON_BLOCK_TEXTS = [
    "No results found for your search.",
    "0 records returned for the selected date range.",
    "Showing 1-25 of 312 documents",
    "DECREE OF DISSOLUTION  SMITH, JOHN",
    "",
    None,
    # These generic words must NOT trip the high-confidence detector (false-fail risk).
    "Access to county records is governed by RCW. Please log in for saved searches.",
    "Forbidden zones are listed in the plat. Session saved.",
    "Service available Monday-Friday.",
]


@pytest.mark.parametrize("text", NON_BLOCK_TEXTS)
def test_detect_block_negatives(text):
    assert detect_block(text) is None


# ── classify_results_page ─────────────────────────────────────────────────────

_MARKERS = ("no results", "0 records", "no documents found")


def test_classify_rows():
    assert classify_results_page(row_count=5, page_text="anything", empty_markers=_MARKERS) == "rows"


def test_classify_rows_wins_even_with_block_text():
    # If we actually extracted rows, the page is fine regardless of stray text.
    assert classify_results_page(row_count=3, page_text="captcha", empty_markers=_MARKERS) == "rows"


def test_classify_block_beats_empty_marker():
    # A block wall that also renders "no results" must NOT be read as genuine empty.
    text = "Attention Required! Cloudflare. No results."
    assert classify_results_page(row_count=0, page_text=text, empty_markers=_MARKERS) == "block"


def test_classify_genuine_empty():
    assert classify_results_page(
        row_count=0, page_text="No results found for that window.", empty_markers=_MARKERS
    ) == "empty"


def test_classify_ambiguous_raises_caller():
    # No rows, no marker, no block -> ambiguous (caller must raise, not return []).
    assert classify_results_page(
        row_count=0, page_text="<html><body></body></html>", empty_markers=_MARKERS
    ) == "ambiguous"


def test_classify_ambiguous_on_empty_text():
    assert classify_results_page(row_count=0, page_text=None, empty_markers=_MARKERS) == "ambiguous"


# ── check_extraction_canary ───────────────────────────────────────────────────

def test_canary_raises_on_header_vs_zero():
    with pytest.raises(ScraperExecutionError):
        check_extraction_canary(
            expected_total=42, raw_extracted=0, county="pierce", platform="ARMS"
        )


def test_canary_silent_when_genuinely_zero():
    # Header reported 0 -> genuinely empty, not a canary trip.
    check_extraction_canary(expected_total=0, raw_extracted=0, county="x", platform="P")


def test_canary_silent_when_header_unknown():
    check_extraction_canary(expected_total=None, raw_extracted=0, county="x", platform="P")


def test_canary_silent_when_rows_extracted():
    check_extraction_canary(expected_total=100, raw_extracted=100, county="x", platform="P")


def test_canary_does_not_trip_on_dedup_shrink():
    # raw_extracted is PRE-dedup; a smaller final emitted count is not its concern.
    check_extraction_canary(expected_total=50, raw_extracted=50, county="x", platform="P")


# ── exception shape ───────────────────────────────────────────────────────────

def test_execution_error_message_includes_context():
    err = ScraperExecutionError(
        "skagit", "SkagitRecording", "search click failed",
        record_type="divorce", doc_type="Decree-divorce", page=2,
        date_from="01/01/2026", date_to="03/01/2026",
    )
    s = str(err)
    assert "skagit" in s and "SkagitRecording" in s and "search click failed" in s
    assert "rt=divorce" in s and "doc_type=Decree-divorce" in s and "page=2" in s


def test_blocked_error_is_execution_error():
    assert issubclass(ScraperBlockedError, ScraperExecutionError)
    assert isinstance(
        ScraperBlockedError("x", "P", "captcha"), ScraperExecutionError
    )


def test_chaining_supported():
    try:
        try:
            raise ValueError("boom")
        except ValueError as exc:
            raise ScraperExecutionError("x", "P", "wrapped") from exc
    except ScraperExecutionError as e:
        assert isinstance(e.__cause__, ValueError)
