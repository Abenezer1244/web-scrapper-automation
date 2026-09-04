"""Tests for King eRealProperty owner-name extraction (pure, no network).

`_extract_owner_name` parses the owner/taxpayer off the eRealProperty Dashboard
page so King tax-delinquent leads get a real name instead of the
"Tax Delinquent — $X owed (Parcel …)" placeholder. The markup samples below are
the REAL shape served by blue.kingcounty.com (captured live), not invented.
"""
import pytest
from sqlalchemy import text

from src.scrapers.enrichment.king_county_assessor import (
    KingOwnerLookupBlockedError,
    _extract_owner_name,
    _extract_parcel_echo,
    _fetch_king_owner,
    batch_enrich_king_county,
    batch_extract_king_owners,
    parcel_page_is_for,
)
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY


@pytest.fixture(autouse=True)
def _reset_source_health():
    """Clear the shared eRealProperty health row around every test.

    batch_extract_king_owners now consults durable source health, and the breaker
    tests below deliberately trip it — which PERSISTS. Without this reset the
    first tripping test would block every later test in the file (and any other
    file touching King enrichment) with SourceUnavailableError. That is the
    feature working; the tests just need to not leak state into each other.
    """
    from src.db.session import SyncSessionLocal

    def _wipe():
        with SyncSessionLocal() as db:
            db.execute(
                text("DELETE FROM external_source_health WHERE source_key = :k"),
                {"k": KING_EREALPROPERTY},
            )
            db.commit()

    _wipe()
    yield
    _wipe()

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


# ── Parcel-echo verification (Test 7 audit, 2026-09-03) ──────────────────────
#
# eRealProperty SILENTLY TRUNCATES an over-length ParcelNbr to the first 10 digits
# and serves a DIFFERENT parcel's page with HTTP 200 and no error. King's own
# recorder emits malformed PIDs in its legal-description index, so this reached
# real leads: requesting 64116000027 (the 11-digit PID printed in the recorder's
# legal for a REINKE death certificate) returns parcel 641160-0002 — SNYDER JACOB's
# house at 11524 MERIDIAN AVE N — and that address was attached to the lead, and
# enqueued for a paid skip trace.
#
# The markup below is the EXACT shape captured live from those pages.

_ECHO_MISMATCH = (
    '<tr class="GridViewRowStyle">\r\n\t\t\t'
    '<td style="font-weight:bold;width:200px;">Parcel Number</td><td>641160-0002</td>\r\n\t\t</tr>'
    '<tr class="GridViewAlternatingRowStyle">\r\n\t\t\t'
    '<td style="font-weight:bold;width:200px;">Name</td><td>SNYDER JACOB                </td>\r\n\t\t</tr>'
    '<tr class="GridViewRowStyle">\r\n\t\t\t'
    '<td style="font-weight:bold;width:200px;">Site Address</td><td>11524 MERIDIAN AVE N 98133</td>\r\n\t\t</tr>'
)
_ECHO_MATCH = (
    '<tr><td style="font-weight:bold;">Parcel Number</td><td>641160-0027</td></tr>'
    '<tr><td style="font-weight:bold;">Name</td><td>REINKE NORMAN L</td></tr>'
    '<tr><td style="font-weight:bold;">Site Address</td><td>11547 CORLISS AVE N 98133</td></tr>'
)


def test_parcel_echo_is_read_from_the_labelled_cell():
    assert _extract_parcel_echo(_ECHO_MISMATCH) == "6411600002"
    assert _extract_parcel_echo(_ECHO_MATCH) == "6411600027"


def test_parcel_echo_ignores_other_numbers_on_the_page():
    # Label-anchored, never "the first 10-digit number in the HTML": the live page
    # is full of unrelated long numbers (tax-bill links, report ids, ZIPs).
    markup = (
        '<a href="https://payment.kingcounty.gov/Home/Index?Search=9999999999">Tax</a>'
        '<span>0123456789</span>'
        '<tr><td>Parcel Number</td><td>327608-0220</td></tr>'
    )
    assert _extract_parcel_echo(markup) == "3276080220"


def test_page_for_a_different_parcel_is_rejected():
    # The exact live defect: we asked about 64116000027, King answered about
    # 641160-0002. Nothing on that page belongs to this lead.
    assert not parcel_page_is_for(_ECHO_MISMATCH, "64116000027")


def test_page_for_the_requested_parcel_is_accepted():
    assert parcel_page_is_for(_ECHO_MATCH, "6411600027")
    # Formatting differences in the echo (King prints major-minor) must not matter.
    assert parcel_page_is_for(
        '<tr><td>Parcel Number</td><td>375160-4519</td></tr>', "3751604519"
    )


def test_missing_echo_is_trusted_only_for_a_well_formed_king_pin():
    # A layout change that drops the Parcel Number cell must not zero out every
    # King lookup — a 10-digit PIN cannot be truncated, so it stays trusted...
    assert parcel_page_is_for("<tr><td>Name</td><td>SMITH JANE</td></tr>", "3751604519")
    # ...but a malformed id with no echo has no evidence at all and fails CLOSED.
    assert not parcel_page_is_for("<tr><td>Name</td><td>SMITH JANE</td></tr>", "64116000027")
    assert not parcel_page_is_for("<tr><td>Name</td><td>SMITH JANE</td></tr>", "012603938700")


async def test_owner_lookup_discards_a_different_parcels_page(monkeypatch):
    # The owner-only path hits the SAME lenient endpoint (Codex P1) and is used by
    # the inline owner repair and two backfill scripts. A wrong owner is worse than
    # no owner, and it is a genuine miss (not transient) — retrying returns the
    # same page.
    class _Resp:
        status_code = 200
        text = _ECHO_MISMATCH

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.safe_get", lambda *a, **k: _Resp()
    )
    assert await _fetch_king_owner("64116000027") == (None, False)


async def test_owner_lookup_keeps_a_matching_parcels_page(monkeypatch):
    class _Resp:
        status_code = 200
        text = _ECHO_MATCH

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.safe_get", lambda *a, **k: _Resp()
    )
    assert await _fetch_king_owner("6411600027") == ("REINKE NORMAN L", False)


async def test_enrichment_attaches_nothing_from_a_different_parcels_page(monkeypatch):
    # End-to-end for the actual Test 7 defect: no property address, no owner, and
    # no tax URL (so no mailing lookup and no skip-trace enqueue, which requires a
    # non-null property address).
    class _Resp:
        status_code = 200
        text = _ECHO_MISMATCH + (
            '<a href="https://payment.kingcounty.gov/Home/Index?'
            'app=PropertyTaxes&amp;Search=64116000027">Property Tax Bill</a>'
        )

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.safe_get", lambda *a, **k: _Resp()
    )
    stats: dict = {}
    out = await batch_enrich_king_county(["64116000027"], stats=stats)
    assert out["64116000027"]["property_address"] is None
    assert out["64116000027"]["owner_name"] is None
    assert out["64116000027"]["mailing_address"] is None
    assert out["64116000027"]["parcel_lookup"] == "mismatch"
    assert stats["parcel_mismatch"] == 1
    assert stats["mailing_candidates"] == 0


def test_an_unreadable_parcel_cell_is_not_treated_as_a_missing_one():
    # Codex P3: a page that CARRIES a parcel cell but declines to name the parcel
    # ("N/A", blank) is not evidence that the page is ours, even for a well-formed
    # 10-digit PIN. Only a page with no parcel cell at all falls back to trust.
    assert not parcel_page_is_for('<tr><td>Parcel Number</td><td>N/A</td></tr>', "3751604519")
    assert not parcel_page_is_for('<tr><td>Parcel Number</td><td></td></tr>', "3751604519")


def test_a_blank_parcel_cell_does_not_mask_a_later_mismatching_one():
    # Codex P3: eRealProperty labels the cell "Parcel" on one view and
    # "Parcel Number" on another, so the first match may be empty. Scan them all.
    markup = (
        '<tr><td>Parcel</td><td>&nbsp;</td></tr>'
        '<tr><td>Parcel Number</td><td>641160-0002</td></tr>'
    )
    assert _extract_parcel_echo(markup) == "6411600002"
    assert not parcel_page_is_for(markup, "64116000027")
    assert parcel_page_is_for(markup, "6411600002")


def test_an_empty_requested_pid_is_never_trusted():
    assert not parcel_page_is_for(_ECHO_MATCH, "")
    assert not parcel_page_is_for("<tr><td>Name</td><td>SMITH JANE</td></tr>", "")
