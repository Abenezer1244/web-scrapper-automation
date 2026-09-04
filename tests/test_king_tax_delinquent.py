"""Tests for the King County tax-delinquent aggregation (pure, no network).

`aggregate_delinquent_rows` is the heart of the rewritten King scraper: it turns
the Socrata "Delinquent Taxes" rows (one unpaid receivable LINE per charge type
per year) into one record per parcel, summing (billed - paid) across all
included charge types and delinquent years — matching Snohomish's methodology.

Fixtures use Socrata-shaped row dicts with zero-padded cent strings, mirroring
the live dsv3-ct3e payload. The HTTP pagination path is exercised by the live
Railway smoke run; here we lock the money math + gating that the old
`receivable_type='D'` bug got wrong.
"""
from decimal import Decimal

import requests

from src.scrapers.king_wa_tax_delinquent import (
    _SOURCE,
    _is_retryable,
    _page_params,
    aggregate_delinquent_rows,
    is_parcel_legal_placeholder,
    is_parse_break,
    is_tax_placeholder_party,
    tax_placeholder_party,
)


def test_placeholder_party_roundtrips_through_matcher():
    # The producer's output must be recognized by the enrichment overwrite gate.
    pn = tax_placeholder_party(Decimal("12345.67"), "1954600115")
    assert pn == "Tax Delinquent — $12,346 owed (Parcel 1954600115)"
    assert is_tax_placeholder_party(pn) is True


def test_legal_placeholder_detects_parcel_stand_in():
    # scrape() sets legal_description = parcel (no legal desc in the Socrata feed),
    # so the stand-in is detected iff legal_description equals the parcel.
    assert is_parcel_legal_placeholder("1954600115", "1954600115") is True
    assert is_parcel_legal_placeholder(" 1954600115 ", "1954600115") is True  # whitespace-tolerant


def test_legal_placeholder_rejects_real_legal_and_blanks():
    # A real legal description must NOT be seen as the placeholder, and blanks/None
    # are never the stand-in (so enrichment never clobbers a real value).
    assert is_parcel_legal_placeholder("LOT 4 BLK 2 GREENBRIDGE DIV NO 3", "1954600115") is False
    assert is_parcel_legal_placeholder(None, "1954600115") is False
    assert is_parcel_legal_placeholder("", "1954600115") is False
    assert is_parcel_legal_placeholder("1954600115", None) is False
    assert is_parcel_legal_placeholder("9999999999", "1954600115") is False  # different parcel
    assert is_parcel_legal_placeholder("   ", "  ") is False  # whitespace-only must not pass


def test_matcher_rejects_real_and_empty_party_names():
    # Real owner names (even one starting with the prefix) and blanks are NOT
    # treated as the placeholder, so enrichment never clobbers a real lead.
    assert is_tax_placeholder_party(None) is False
    assert is_tax_placeholder_party("") is False
    assert is_tax_placeholder_party("TOMLINSON WILLIAM+CHERYL L") is False
    assert is_tax_placeholder_party("Tax Delinquent Holdings LLC") is False
    # Contrived name with the prefix + "owed (Parcel …)" but NO "$amount" clause
    # must not match (the anchored amount requirement defeats it).
    assert is_tax_placeholder_party(
        "Tax Delinquent Holdings LLC owed (Parcel Services)"
    ) is False


def _row(acct, year, rtype, billed, paid):
    """Build a Socrata-shaped row dict (cent strings, zero-padded like live)."""
    return {
        "account_number": acct,
        "bill_year": str(year),
        "receivable_type": rtype,
        "billed_amount": f"{billed:010d}",
        "paid_amount": f"{paid:010d}",
    }


# Parcel A (0123456789): delinquent across 2023+2024, multiple charge types,
# spread over TWO 12-digit accounts that share the same 10-digit parcel; plus an
# abatement line (excluded) and an unknown-code line (excluded).
# Parcel B: single year, single charge. Parcel C: net-zero (fully paid) -> dropped.
FIXTURE_ROWS = [
    _row("012345678900", 2024, "R", 1418785, 0),        # $14,187.85 main tax
    _row("012345678900", 2024, "U", 122553, 0),         # $1,225.53 surface water
    _row("012345678900", 2024, "D", 9152, 0),           # $91.52 drainage
    _row("012345678900", 2023, "R", 1000000, 200000),   # $8,000.00 (partial pay)
    _row("012345678900", 2024, "A", 50000000, 0),       # abatement -> EXCLUDED
    _row("012345678900", 2024, "Z", 5000, 0),           # unknown code -> EXCLUDED
    _row("012345678955", 2024, "N", 831, 0),            # $8.31, SAME parcel A
    _row("022222222200", 2024, "R", 300000, 0),         # Parcel B $3,000.00
    _row("033333333300", 2024, "R", 100000, 100000),    # Parcel C net $0 -> drop
    # malformed accounts (quarantined, never collapse into a parcel)
    _row("0044444", 2024, "R", 5000, 0),                # 7-digit personal property
    _row("ABCDEFGHIJKL", 2024, "R", 5000, 0),           # 12 chars but non-numeric
    {"account_number": "012345678977", "bill_year": "notayear",
     "receivable_type": "R", "billed_amount": "0000005000", "paid_amount": "0000000000"},
    # out-of-range years (excluded by the year window, not counted malformed)
    _row("055555555500", 2099, "R", 5000, 0),           # future
    _row("066666666600", 2019, "R", 5000, 0),           # before start
]


def _by_parcel(records):
    return {r.parcel_id: r for r in records}


def test_aggregates_all_charge_types_and_years_per_parcel():
    records, stats = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    by = _by_parcel(records)

    # Only the two parcels with net-positive owed; net-zero parcel C dropped.
    assert set(by) == {"0123456789", "0222222222"}

    a = by["0123456789"].enrichment_data
    # 14187.85 + 1225.53 + 91.52 (2024, acct ...00) + 8000.00 (2023) + 8.31 (acct ...55)
    assert a["delinquent_amount"] == "23513.21"
    assert Decimal(a["delinquent_amount"]) == Decimal("23513.21")  # no float drift
    assert a["bill_year"] == 2023            # oldest delinquent year
    assert a["oldest_tax_year"] == 2023
    assert a["delinquent_years"] == [2023, 2024]
    assert a["delinquent_year_count"] == 2
    assert a["source"] == "king_county_delinquent_taxes"
    # NO fabricated date: the tax receivable roll has no filing/recording date, so
    # date_recorded stays NULL. The bill year is surfaced as oldest_tax_year.
    assert by["0123456789"].date_recorded is None
    # both 12-digit accounts that share the parcel are recorded
    assert a["account_numbers"] == ["012345678900", "012345678955"]


def test_charge_type_and_year_breakdown_kept():
    records, _ = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    a = _by_parcel(records)["0123456789"].enrichment_data
    # R = 14187.85 (2024) + 8000.00 (2023) = 22187.85; abatement/unknown excluded.
    assert a["amount_by_charge_type"] == {
        "D": "91.52", "N": "8.31", "R": "22187.85", "U": "1225.53",
    }
    assert "A" not in a["amount_by_charge_type"]
    assert "Z" not in a["amount_by_charge_type"]
    assert a["amount_by_year"] == {"2023": "8000.00", "2024": "15513.21"}


def test_abatement_and_unknown_excluded_and_alerted():
    _, stats = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    assert stats["abatement_rows"] == 1
    assert stats["abatement_nonzero"] == 1          # the $500k A row -> alert
    assert stats["unknown_type_rows"] == 1
    assert stats["unknown_codes"] == {"Z"}


def test_malformed_accounts_quarantined():
    _, stats = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    # 7-digit, non-numeric, and bad-year rows are quarantined, never parcels.
    assert stats["skipped_malformed_acct"] == 3
    assert stats["total_rows"] == len(FIXTURE_ROWS)


def test_single_charge_parcel_amount_and_label():
    records, _ = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    b = _by_parcel(records)["0222222222"]
    assert b.enrichment_data["delinquent_amount"] == "3000.00"
    assert b.enrichment_data["bill_year"] == 2024
    assert b.parcel_id == "0222222222"
    # No fabricated owner name — blank (honest); real name comes from skip-trace/eRealProperty.
    assert b.party_name is None
    # doc_type stays None (like Snoho tax) so cached-records `doc_type IS NULL`
    # keeps these rows visible.
    assert b.doc_type is None


def test_current_year_and_out_of_range_excluded():
    # effective_end_year=2024 must drop the 2099 future row; start_year=2020 drops
    # the 2019 row. Neither should create a parcel.
    records, _ = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    by = _by_parcel(records)
    assert "0555555555" not in by   # 2099 future bill
    assert "0666666666" not in by   # 2019 before window


def test_empty_input_is_clean():
    records, stats = aggregate_delinquent_rows(
        [], start_year=2020, effective_end_year=2024
    )
    assert records == []
    assert stats["total_rows"] == 0
    assert stats["unknown_codes"] == set()


# ─── 18-month product cap (drop parcels whose OLDEST unpaid year is too old) ───

# Parcel OLD (oldest year 2010) vs Parcel NEW (oldest year 2025). The cap drops
# a parcel by its OLDEST delinquent year, so OLD must go even though it also
# carries a 2025 line (recency-over-volume trade, user decision 2026-06-16).
_CAP_ROWS = [
    _row("011111111100", 2010, "R", 100000, 0),   # Parcel OLD: oldest = 2010
    _row("011111111100", 2025, "R", 200000, 0),   # ...also delinquent 2025
    _row("022222222200", 2025, "R", 300000, 0),   # Parcel NEW: oldest = 2025
]


def test_cap_drops_parcel_with_old_oldest_year():
    records, stats = aggregate_delinquent_rows(
        _CAP_ROWS, start_year=2000, effective_end_year=2025, cap_min_year=2025
    )
    by = _by_parcel(records)
    assert set(by) == {"0222222222"}          # NEW kept
    assert "0111111111" not in by             # OLD dropped (oldest 2010 < 2025)
    assert stats["capped_out"] == 1
    assert by["0222222222"].enrichment_data["bill_year"] == 2025


def test_cap_keeps_parcel_at_boundary_year():
    # A parcel whose oldest year EQUALS cap_min_year is kept (>= cutoff).
    rows = [_row("033333333300", 2025, "R", 100000, 0)]
    records, stats = aggregate_delinquent_rows(
        rows, start_year=2000, effective_end_year=2025, cap_min_year=2025
    )
    assert _by_parcel(records)["0333333333"].enrichment_data["bill_year"] == 2025
    assert stats["capped_out"] == 0


# ─── King is EXEMPT from the recency cap (user decision 2026-06-23) ────────────

def test_king_source_is_cap_exempt():
    # The ingestion exemption is driven by membership in TAX_CAP_EXEMPT_SOURCES,
    # keyed on the EXACT source the King scraper stamps — so ingestion and the
    # query-side cap can never disagree.
    from src.api.tax_filters import TAX_CAP_EXEMPT_SOURCES

    assert _SOURCE == "king_county_delinquent_taxes"
    assert _SOURCE in TAX_CAP_EXEMPT_SOURCES


def test_cap_none_keeps_old_king_parcels():
    # With the cap disabled (King's ingestion path: cap_min_year=None), a parcel
    # whose oldest unpaid year is far back is KEPT — King's feed lists only
    # currently-unpaid receivables, so an old bill_year is still a live lead.
    records, stats = aggregate_delinquent_rows(
        _CAP_ROWS, start_year=2000, effective_end_year=2025, cap_min_year=None
    )
    by = _by_parcel(records)
    assert "0111111111" in by                  # oldest 2010 — kept (no cap)
    assert by["0111111111"].enrichment_data["bill_year"] == 2010
    assert stats["capped_out"] == 0


# ─── aggregation funnel stats (drive the structural canary) ───────────────────

def test_aggregated_parcels_and_net_zero_counts():
    rows = [
        _row("011111111100", 2024, "R", 100000, 0),    # owed -> candidate, emitted
        _row("022222222200", 2024, "R", 100000, 100000),  # net zero -> candidate, dropped
    ]
    records, stats = aggregate_delinquent_rows(
        rows, start_year=2000, effective_end_year=2024
    )
    assert stats["aggregated_parcels"] == 2   # both formed candidates
    assert stats["net_zero_parcels"] == 1     # one fully paid -> dropped
    assert {r.parcel_id for r in records} == {"0111111111"}


# ─── structural canary: is_parse_break (Codex-reviewed funnel) ────────────────

def _canary_stats(**over):
    base = {"total_rows": 200, "aggregated_parcels": 50, "overflow": 0,
            "capped_out": 0, "net_zero_parcels": 0}
    base.update(over)
    return base


def test_canary_no_raise_when_records_emitted():
    # Any emitted record means the parse worked — never a break.
    assert is_parse_break(_canary_stats(), n_emitted=10) is False


def test_canary_no_raise_on_small_scan():
    # A tiny scan that yields nothing is legitimate (below the threshold).
    assert is_parse_break(_canary_stats(total_rows=50, aggregated_parcels=0), 0) is False


def test_canary_raises_when_nothing_parsed():
    # Rows scanned but ZERO candidates -> every row hit a row-level gate
    # (malformed/abatement/unknown) = schema/format drift -> parse break.
    assert is_parse_break(_canary_stats(aggregated_parcels=0), 0) is True


def test_canary_legitimate_empty_all_capped_does_not_raise():
    # Candidates existed but were all removed by the recency cap -> legitimate
    # business empty (the exact King pre-fix scenario) -> must NOT raise.
    assert is_parse_break(_canary_stats(aggregated_parcels=50, capped_out=50), 0) is False


def test_canary_legitimate_empty_all_net_zero_does_not_raise():
    assert is_parse_break(_canary_stats(aggregated_parcels=50, net_zero_parcels=50), 0) is False


def test_canary_raises_when_all_candidates_overflow():
    # Every candidate was an absurd value -> unit change / corruption -> raise.
    assert is_parse_break(_canary_stats(aggregated_parcels=50, overflow=50), 0) is True


def test_canary_partial_overflow_with_cap_does_not_raise():
    # Overflow present but NOT total (rest capped) -> still a legitimate empty.
    assert is_parse_break(
        _canary_stats(aggregated_parcels=50, overflow=2, capped_out=48), 0
    ) is False


def test_cap_none_disables_cap_back_compat():
    # cap_min_year=None (default) must leave every parcel — back-compat.
    records, stats = aggregate_delinquent_rows(
        _CAP_ROWS, start_year=2000, effective_end_year=2025
    )
    by = _by_parcel(records)
    assert set(by) == {"0111111111", "0222222222"}
    assert stats["capped_out"] == 0
    assert by["0111111111"].enrichment_data["bill_year"] == 2010


# --- Socrata pagination: MUST order by the unique, indexed :id ---------------
# account_number,bill_year is NON-unique (a parcel has many same-account/year
# charge LINES). Under $offset paging, tied rows reorder at page boundaries and
# can be skipped or duplicated — the exact rows being summed — silently
# undercounting/overcounting a parcel. :id is unique+indexed: stable paging AND
# ~24x faster (no server-side sort of the filtered set, which read-timed-out).

def test_page_params_order_by_id_for_stable_offset_paging():
    p = _page_params("bill_year>='2025'", offset=10000)
    assert p["$order"] == ":id", "offset paging requires the unique :id order"
    assert p["$where"] == "bill_year>='2025'"
    assert p["$offset"] == 10000
    assert p["$limit"] > 0


def test_retryable_classification():
    # Transient → retry: read timeout, connection drop, 429, 5xx.
    assert _is_retryable(requests.exceptions.ReadTimeout())
    assert _is_retryable(requests.exceptions.ConnectionError())
    for code in (429, 500, 502, 503, 504):
        err = requests.exceptions.HTTPError()
        err.response = requests.Response()
        err.response.status_code = code
        assert _is_retryable(err), f"{code} should retry"
    # Non-transient → fail loud: 4xx (bad query/forbidden) and SSRF refusal.
    for code in (400, 403, 404):
        err = requests.exceptions.HTTPError()
        err.response = requests.Response()
        err.response.status_code = code
        assert not _is_retryable(err), f"{code} must not retry"
    assert not _is_retryable(ValueError("SSRF: refusing internal host"))


# ── Window truncation regression (Test 10) ────────────────────────────────────
# Reproduces the SOURCE SHAPE that caused it: King's feed carries a parcel's
# delinquent charge lines for years reaching far back (live: 2002..2026), while a
# job asks for a recent window. The window must decide WHICH PARCELS are leads —
# never how much a selected lead owes or how far back its delinquency runs.
# Before the fix, a lower bound in the Socrata $where (mirrored by a lower-bound
# row drop here) hid the pre-window years: on a real 384-lead King job that made
# 100 leads (26%) report a too-recent oldest year and understated the delinquent
# balance by $652,958.57 in aggregate.

# Parcel LONGRUN is delinquent 2021-2026; parcel RECENT only in 2026.
_WINDOW_ROWS = [
    _row("111111111100", 2021, "R", 150000, 0),   # $1,500.00  pre-window
    _row("111111111100", 2022, "R", 160000, 0),   # $1,600.00  pre-window
    _row("111111111100", 2023, "N", 2500, 0),     # $25.00     pre-window
    _row("111111111100", 2025, "R", 200000, 0),   # $2,000.00  IN window
    _row("111111111100", 2026, "R", 210000, 0),   # $2,100.00  IN window
    _row("222222222200", 2026, "R", 90000, 0),    # $900.00    IN window only
]


def test_balance_and_oldest_year_span_all_years_not_just_the_window():
    records, stats = aggregate_delinquent_rows(
        _WINDOW_ROWS, start_year=2025, effective_end_year=2026
    )
    by = _by_parcel(records)
    assert set(by) == {"1111111111", "2222222222"}

    ed = by["1111111111"].enrichment_data
    # 1500 + 1600 + 25 + 2000 + 2100 — the pre-window years are REAL money owed.
    assert ed["delinquent_amount"] == "7225.00"
    assert ed["oldest_tax_year"] == 2021          # not 2025
    assert ed["bill_year"] == 2021
    assert ed["delinquent_years"] == [2021, 2022, 2023, 2025, 2026]
    assert ed["delinquent_year_count"] == 5
    # Per-year breakdown keeps every year, so the UI can show the full history.
    assert ed["amount_by_year"]["2021"] == "1500.00"
    assert ed["amount_by_year"]["2026"] == "2100.00"

    # A parcel with only in-window delinquency is unaffected.
    assert by["2222222222"].enrichment_data["delinquent_amount"] == "900.00"
    assert by["2222222222"].enrichment_data["oldest_tax_year"] == 2026
    assert stats["out_of_window"] == 0


def test_parcel_delinquent_only_before_the_window_is_not_a_lead():
    # Selection still honours the window: pre-window-only delinquency is read (it
    # would count toward an in-window parcel's total) but never emitted as a lead.
    rows = [
        _row("333333333300", 2021, "R", 500000, 0),
        _row("333333333300", 2022, "R", 500000, 0),
    ]
    records, stats = aggregate_delinquent_rows(
        rows, start_year=2025, effective_end_year=2026
    )
    assert records == []
    assert stats["out_of_window"] == 1
    # It never had an in-window charge line, so it is not a CANDIDATE either: the
    # canary must not count history-only parcels, or stale old rows would keep it
    # quiet while schema drift silently wiped the current window's whole lead set.
    assert stats["aggregated_parcels"] == 0
    assert stats["net_zero_parcels"] == 0  # not "fully paid" — simply not in scope


def test_future_bill_years_never_inflate_the_balance():
    # Years past the effective end are not yet billed and must be ignored even
    # though the lower bound is gone.
    rows = [
        _row("444444444400", 2026, "R", 100000, 0),
        _row("444444444400", 2027, "R", 999999, 0),   # future -> excluded
    ]
    records, _ = aggregate_delinquent_rows(
        rows, start_year=2025, effective_end_year=2026
    )
    ed = _by_parcel(records)["4444444444"].enrichment_data
    assert ed["delinquent_amount"] == "1000.00"
    assert ed["delinquent_years"] == [2026]


def test_tax_rows_never_carry_a_fabricated_date():
    # The Socrata tax roll exposes a bill YEAR and no event date. Every emitted
    # record must leave date_recorded NULL rather than synthesize a January 1st.
    records, _ = aggregate_delinquent_rows(
        _WINDOW_ROWS, start_year=2025, effective_end_year=2026
    )
    assert records
    assert all(r.date_recorded is None for r in records)


def test_old_debt_cannot_resurrect_a_parcel_square_for_the_window():
    # Codex P1 on the full-history change: if mere PRESENCE of an in-window line
    # selected the parcel, a parcel whose in-window years are fully PAID would be
    # dragged back into the lead set by its pre-window debt — silently changing
    # WHICH parcels are leads (and what gets billed), not just their totals.
    # Selection is net-owed-in-window, exactly the test the pre-fix code applied.
    rows = [
        _row("777777777700", 2023, "R", 400000, 0),        # $4,000 pre-window debt
        _row("777777777700", 2026, "R", 100000, 100000),   # in-window, fully PAID
    ]
    records, stats = aggregate_delinquent_rows(
        rows, start_year=2025, effective_end_year=2026
    )
    assert records == []
    # It IS a candidate (it had an in-window line), just a fully-paid one — so it
    # is reported as net-zero, not as "no in-window activity".
    assert stats["aggregated_parcels"] == 1
    assert stats["net_zero_parcels"] == 1
    assert stats["out_of_window"] == 0


def test_partially_paid_in_window_still_leads_and_carries_old_debt():
    # The mirror case: a parcel that still owes something in-window IS a lead, and
    # its balance/oldest year then legitimately include the pre-window years.
    rows = [
        _row("888888888800", 2023, "R", 400000, 0),        # $4,000 pre-window
        _row("888888888800", 2026, "R", 100000, 60000),    # $400 still owed
    ]
    records, stats = aggregate_delinquent_rows(
        rows, start_year=2025, effective_end_year=2026
    )
    ed = _by_parcel(records)["8888888888"].enrichment_data
    assert ed["delinquent_amount"] == "4400.00"
    assert ed["oldest_tax_year"] == 2023
    assert stats["net_zero_parcels"] == 0
