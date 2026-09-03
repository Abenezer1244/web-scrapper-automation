"""Regressions from the 2026-09-02 Pierce auction-leads ("Test 3") data-quality audit.

Two REAL Tacoma Daily Index notices, fetched 2026-09-02 and saved verbatim as
fixtures (no mocks):

* ``nts_tacoma_matured_obligation.txt`` — TS# WA-26-1050840-BB, a Quality Loan
  COMMERCIAL / matured-loan notice whose section IV reads "The sum owing on the
  matured obligation secured by the Deed of Trust is: $575,150.38." (no "principal"
  wording). The old regex returned None and the lead shipped with a blank Default
  Owed while the source plainly stated the amount.
* ``nts_tacoma_paren_grantor.txt`` — TS# WA-26-1048713-RM, whose grantor carries a
  title-exception note in parentheses: "BARBARA J. HILL, AS SURVIVING SPOUSE
  ( SUBJECT TO SCH. B, 4 A )". The shared label stop fired on "SUBJECT TO" inside
  the parenthetical and the lead's party_name shipped as "… SURVIVING SPOUSE (".

Parcels stay exactly as the trustee printed them ("602543-087-0" is a real Pierce
spelling of 6025430870): every consumer normalises for matching, and the county-GIS
mapping fix (tests/test_county_gis_batch_mapping.py) is what makes the dashed
spelling enrich correctly.
"""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.scrapers.preforeclosure import strip_vesting_clause
from src.scrapers.sources.nts_tacoma_index import (
    _principal_owing,
    notice_to_row,
    parse_nts_notice,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class TestMaturedObligationNotice:
    def setup_method(self):
        self.p = parse_nts_notice(_load("nts_tacoma_matured_obligation.txt"))

    def test_amount_captured_without_principal_wording(self):
        assert self.p["principal_owing"] == Decimal("575150.38")

    def test_identity_fields_unchanged(self):
        assert self.p["ts_number"] == "WA-26-1050840-BB"
        assert self.p["parcel"] == "0220104064"  # leading zero kept
        assert self.p["auction_date"] == "9/4/2026"
        assert "CN Foods LLC" in self.p["grantor"]
        assert self.p["beneficiary"] == "CASCARA CAPITAL, LLC"

    def test_row_carries_amount_and_parcel(self):
        row = notice_to_row(self.p, "https://example.test/x", date(2026, 9, 2))
        assert row is not None
        assert row["principal_owing"] == Decimal("575150.38")
        assert row["parcel"] == "0220104064"
        assert row["is_active"] is True


class TestSectionIvAmountPinning:
    """The amount must stay pinned inside section IV and prefer the principal label."""

    def test_principal_preferred_over_an_earlier_figure(self):
        text = (
            "IV. The sum owing on the obligation secured by the Deed of Trust is: "
            "accrued interest of $1,234.56 plus the principal sum of $200,000.00, "
            "together with interest as provided in the Note. V. The above-described"
        )
        assert _principal_owing(text) == Decimal("200000.00")

    def test_first_figure_only_when_no_principal_label(self):
        text = (
            "IV. The sum owing on the matured obligation secured by the Deed of Trust "
            "is: $575,150.38. V. The above-described real property will be sold"
        )
        assert _principal_owing(text) == Decimal("575150.38")

    def test_north_star_phrasing_and_plural_deeds(self):
        text = (
            "IV. The sum owing on the obligation secured by the Deeds of Trust is: "
            "Principal $310,000.00, together with interest as provided in the note"
        )
        assert _principal_owing(text) == Decimal("310000.00")

    def test_no_drift_into_section_v(self):
        text = (
            "IV. The sum owing on the obligation secured by the Deed of Trust is: "
            "as set out in the attached statement. V. The sale will be made for a "
            "minimum bid of $10,000.00 without warranty"
        )
        assert _principal_owing(text) is None

    def test_first_figure_cap_without_section_marker(self):
        # No "V." marker and no principal label: a figure 100+ chars after "is:" is
        # not section IV's sum owing — refuse rather than guess.
        text = (
            "IV. The sum owing on the obligation secured by the Deed of Trust is: "
            + "x" * 100
            + " $9,999.99"
        )
        assert _principal_owing(text) is None

    def test_absent_sentence(self):
        assert _principal_owing("NOTICE OF TRUSTEE'S SALE with no section IV at all") is None

    def test_punctuated_qualifier_and_tight_section_marker(self):
        # "matured/commercial" qualifier + "V.The" (no space) still cuts section IV,
        # so the principal-labelled figure in section V is NOT picked (Codex).
        text = (
            "IV. The sum owing on the matured/commercial obligation secured by the Deed "
            "of Trust is: $575,150.38.V.The above-described real property will be sold; "
            "the principal balance of $1.00 stated here belongs to section V."
        )
        assert _principal_owing(text) == Decimal("575150.38")

    def test_connecting_legal_text_tolerated(self):
        # The old regex accepted anything between "obligation" and "principal"; the
        # anchor must stay that tolerant (Codex).
        text = (
            "IV. The sum owing on the obligation, as evidenced by the Note and secured by "
            "the Deed of Trust, is: Principal $185,895.06, together with interest"
        )
        assert _principal_owing(text) == Decimal("185895.06")

    def test_unclosed_paren_note_bounded(self):
        # A malformed cached value: the note never closes, so only a SHORT tail may
        # be treated as the note — a long one is left alone rather than eaten.
        assert strip_vesting_clause("JANE ROE ( SUBJECT TO SCH. B, 4 A") == "JANE ROE"
        long_tail = "JANE ROE ( SUBJECT TO " + "X" * 100
        assert strip_vesting_clause(long_tail) == long_tail

    def test_every_existing_fixture_parses_exactly_as_before(self):
        # The pre-fix regex, verbatim. Wherever it found an amount, the new parser
        # must return the same one — no silent re-interpretation of the historical
        # layouts (North Star, Quality Loan, Clear Recon, worded-date variants).
        old = re.compile(
            r"sum\s+owing\s+on\s+the\s+obligation[^$]*?principal[^$]*?\$([\d,]+\.\d{2})",
            re.I | re.S,
        )
        checked = 0
        for path in sorted(_FIXTURES.glob("nts_tacoma_*.txt")):
            text = path.read_text(encoding="utf-8")
            m = old.search(text)
            if not m:
                continue
            assert _principal_owing(text) == Decimal(m.group(1).replace(",", "")), path.name
            checked += 1
        assert checked >= 3  # the historical layouts are really exercised


class TestParenGrantorNotice:
    def setup_method(self):
        self.p = parse_nts_notice(_load("nts_tacoma_paren_grantor.txt"))

    def test_grantor_runs_through_the_parenthetical(self):
        assert self.p["grantor"] == "BARBARA J. HILL, AS SURVIVING SPOUSE ( SUBJECT TO SCH. B, 4 A )"

    def test_next_labels_not_swallowed(self):
        assert self.p["beneficiary"] == "NEW DAY FINANCIAL, LLC"
        assert self.p["trustee"] == "QUALITY LOAN SERVICE CORPORATION"

    def test_other_fields(self):
        assert self.p["ts_number"] == "WA-26-1048713-RM"
        assert self.p["parcel"] == "7816200340"
        assert self.p["principal_owing"] == Decimal("317785.61")

    def test_lead_name_is_the_owner(self):
        assert strip_vesting_clause(self.p["grantor"]) == "BARBARA J. HILL"

    def test_address_stop_still_works(self):
        # "Subject to" outside parentheses must still terminate the address value.
        assert self.p["property_address"] == "22109 43RD AVENUE EAST, SPANAWAY, WA 98387"


class TestStripVestingClauseParenAndSurvivingSpouse:
    def test_already_cached_truncated_value_cleans_up(self):
        # What the old parser stored (and what shipped as party_name).
        assert strip_vesting_clause("BARBARA J. HILL, AS SURVIVING SPOUSE (") == "BARBARA J. HILL"

    def test_surviving_spouse_with_article(self):
        assert strip_vesting_clause("JANE ROE, AS THE SURVIVING SPOUSE") == "JANE ROE"

    def test_title_note_dropped_but_trustee_vesting_kept(self):
        assert (
            strip_vesting_clause("JOHN DOE, AS TRUSTEE OF THE DOE FAMILY TRUST (SUBJECT TO SCH. B)")
            == "JOHN DOE, AS TRUSTEE OF THE DOE FAMILY TRUST"
        )

    def test_other_parentheticals_untouched(self):
        assert strip_vesting_clause("JOHN DOE (AKA JOHN R DOE)") == "JOHN DOE (AKA JOHN R DOE)"

    def test_clean_name_untouched(self):
        assert strip_vesting_clause("MICHAEL A. BRANDT") == "MICHAEL A. BRANDT"
