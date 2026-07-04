"""NTS grantor label-bleed cleanup — a second Pierce layout appends
"Grantee(s): <trustee> ... Original beneficiary of the deed of trust: <lender>"
onto the owner name. Fixed at the parser (_STOP) + read time (strip_vesting_clause).
Uses REAL stored grantor strings from prod nts_notices (no mocks).
"""
from src.scrapers.preforeclosure import strip_trailing_labels, strip_vesting_clause
from src.scrapers.sources.nts_tacoma_index import parse_nts_notice

# Real messy grantor values as the crawler stored them (verified in prod 2026-07-03).
_REAL = {
    "DEONDRE E. JAMES AND SHAUNIE J. WHEELER-JAMES, HUSBAND AND WIFE Grantee(s): "
    "FIRST AMERICAN TITLE , as Trustee Original beneficiary of the deed of trust: "
    "MORTGAGE ELECTRONIC REGISTRATION SYSTEMS, INC., AS DESIGNATED NOMINEE FOR ROCKET "
    "MORTGAGE, LLC, BENEFICIARY OF THE SECURITY INSTRUMENT": "DEONDRE E. JAMES AND SHAUNIE J. WHEELER-JAMES",
    "WANDA PLEASANT, AN UNMARRIED PERSON Grantee(s): COMMONWEALTH LAND TITLE COMPANY, "
    "as Trustee Original beneficiary of the deed of trust: MERS": "WANDA PLEASANT",
    "VICTOR MATOSICH, AS HIS SEPARATE PROPERTY Grantee(s): RECONTRUST COMPANY, N.A., "
    "as Trustee Original beneficiary of the deed of trust: BANK OF AMERICA, N.A": "VICTOR MATOSICH",
}


class TestStripTrailingLabels:
    def test_cuts_at_grantee_label(self):
        assert strip_trailing_labels("JANE ROE Grantee(s): FIRST AMERICAN TITLE") == "JANE ROE"

    def test_cuts_at_original_beneficiary_label(self):
        assert (
            strip_trailing_labels("JANE ROE Original beneficiary of the deed of trust: MERS")
            == "JANE ROE"
        )

    def test_leaves_clean_name_untouched(self):
        assert strip_trailing_labels("MICHAEL A. BRANDT") == "MICHAEL A. BRANDT"

    def test_does_not_cut_owner_that_is_a_trust_trustee(self):
        # A real owner can be a trust trustee — must NOT be truncated at "as Trustee".
        name = "JOHN DOE, AS TRUSTEE OF THE DOE FAMILY TRUST"
        assert strip_trailing_labels(name) == name


class TestStripVestingClauseOnRealData:
    def test_real_bled_grantors_reduce_to_owner(self):
        for raw, expected in _REAL.items():
            assert strip_vesting_clause(raw) == expected


class TestParserStopsBeforeGrantee:
    def test_grantor_value_stops_before_grantee_label(self):
        text = (
            "NOTICE OF TRUSTEE'S SALE T.S. No.: WA-24-1234 "
            "Grantor(s): JANE Q. HOMEOWNER, A SINGLE PERSON "
            "Grantee(s): FIRST AMERICAN TITLE, as Trustee "
            "Original beneficiary of the deed of trust: SOME BANK, N.A. "
            "will sell at public auction on 8/15/2026"
        )
        parsed = parse_nts_notice(text)
        g = parsed.get("grantor") or ""
        assert "JANE Q. HOMEOWNER" in g
        assert "Grantee" not in g  # value stopped before the next label
        assert "FIRST AMERICAN" not in g
