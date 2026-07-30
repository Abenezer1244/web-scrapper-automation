"""Tests for King eRealProperty owner-name extraction (pure, no network).

`_extract_owner_name` parses the owner/taxpayer off the eRealProperty Dashboard
page so King tax-delinquent leads get a real name instead of the
"Tax Delinquent — $X owed (Parcel …)" placeholder. The markup samples below are
the REAL shape served by blue.kingcounty.com (captured live), not invented.
"""
from src.scrapers.enrichment.king_county_assessor import (
    KingOwnerLookupBlockedError,
    _extract_owner_name,
    batch_extract_king_owners,
)

# Exact markup captured from a live Dashboard page (parcel 1954600115). King
# joins co-owners with "+" and bolds the label cell.
_REAL = (
    '<tr><td style="font-weight:bold;">Name</td>'
    "<td>TOMLINSON WILLIAM+CHERYL L</td></tr>"
)


def test_extracts_real_owner_name():
    assert _extract_owner_name(_REAL) == "TOMLINSON WILLIAM+CHERYL L"


def test_tolerates_whitespace_and_newlines_between_cells():
    markup = "<td>  Name  </td>\n\t<td>\n  SMITH JOHN A  \n</td>"
    assert _extract_owner_name(markup) == "SMITH JOHN A"


def test_case_insensitive_label():
    assert _extract_owner_name("<TD>name</TD><TD>DOE JANE</TD>") == "DOE JANE"


def test_decodes_html_entities():
    markup = "<td>Name</td><td>BARNES &amp; NOBLE LLC&nbsp;</td>"
    assert _extract_owner_name(markup) == "BARNES & NOBLE LLC"


def test_numeric_entity_is_decoded():
    markup = "<td>Name</td><td>O&#39;BRIEN PATRICK</td>"
    assert _extract_owner_name(markup) == "O'BRIEN PATRICK"


def test_entity_owner_is_kept():
    # LLC/estate owners are valid tax-delinquent leads — no orientation applied.
    markup = "<td>Name</td><td>ACME HOLDINGS LLC</td>"
    assert _extract_owner_name(markup) == "ACME HOLDINGS LLC"


def test_missing_name_cell_returns_none():
    assert _extract_owner_name("<td>Site Address</td><td>123 MAIN ST</td>") is None


def test_blank_owner_returns_none():
    assert _extract_owner_name("<td>Name</td><td>&nbsp;</td>") is None


def test_junk_placeholder_returns_none():
    # Punctuation/spacing variants all normalize to a junk token and are dropped.
    for junk in ("N/A", "N.A.", "N / A", "NONE", "Unknown", "null"):
        assert _extract_owner_name(f"<td>Name</td><td>{junk}</td>") is None


def test_first_name_cell_wins_when_unrelated_name_labels_follow():
    # The owner row appears first; a later unrelated "Name" cell must not shadow it.
    markup = (
        "<td>Name</td><td>OWNER OF RECORD</td>"
        "<td>District Name</td><td>SEATTLE SD</td>"
    )
    assert _extract_owner_name(markup) == "OWNER OF RECORD"


# ── batch_extract_king_owners: owner-only HTTP helper (PR #80 reach fix) ──────
# These exercise the input-guard path only — empty / too-short parcel lists are
# filtered BEFORE any HTTP, so they run with no network (no mocks needed).

async def test_batch_extract_king_owners_empty_input_makes_no_requests():
    assert await batch_extract_king_owners([]) == {}


async def test_batch_extract_king_owners_filters_short_and_blank_parcels():
    # All entries are < 6 chars (or blank) and are dropped before any lookup,
    # so the result is empty and no request is ever issued.
    assert await batch_extract_king_owners(["", "   ", "ab", "12345"]) == {}


async def test_batch_extract_king_owners_filters_non_numeric_parcels():
    # A long but digit-less value is rejected by the numeric guard before any HTTP,
    # so no external request is generated for a malformed parcel.
    assert await batch_extract_king_owners(["abcdef", "no-digits-here"]) == {}


async def test_batch_extract_king_owners_trips_on_transient_window(monkeypatch):
    async def blocked(_pid, *, max_attempts=1):
        return None, True

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor._fetch_king_owner",
        blocked,
    )

    parcels = [f"12345678{i:02d}" for i in range(20)]
    try:
        await batch_extract_king_owners(
            parcels,
            delay=0,
            circuit_window=10,
            max_transient_rate=0.10,
        )
    except KingOwnerLookupBlockedError as exc:
        assert "circuit breaker tripped" in str(exc)
    else:
        raise AssertionError("expected KingOwnerLookupBlockedError")


async def test_batch_extract_king_owners_trips_on_miss_window(monkeypatch):
    async def missing(_pid, *, max_attempts=1):
        return None, False

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor._fetch_king_owner",
        missing,
    )

    parcels = [f"12345678{i:02d}" for i in range(20)]
    try:
        await batch_extract_king_owners(
            parcels,
            delay=0,
            circuit_window=10,
            max_transient_rate=1.0,
            max_unresolved_rate=0.50,
        )
    except KingOwnerLookupBlockedError as exc:
        assert "unresolved_rate" in str(exc)
    else:
        raise AssertionError("expected KingOwnerLookupBlockedError")


async def test_batch_extract_king_owners_allows_sparse_real_misses(monkeypatch):
    async def mostly_found(pid, *, max_attempts=1):
        if pid.endswith("00") or pid.endswith("37"):
            return None, False
        return f"OWNER {pid}", False

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor._fetch_king_owner",
        mostly_found,
    )

    parcels = [f"12345678{i:02d}" for i in range(100)]
    owners = await batch_extract_king_owners(parcels, delay=0, circuit_window=50)

    assert len(owners) == 98
    assert "1234567800" not in owners
