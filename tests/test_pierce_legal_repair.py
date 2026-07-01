"""Pierce probate legal-description parcel repair — pure-function guards.

No DB, no network (the HTTP paths are exercised live during development; these
tests lock the parsing/filtering/suffix logic that decides whether a repair is
safe). Regression guard for the live HANSON case: scraped parcel 6779000110
(nonexistent) + legal "PARKWOOD LT 11" -> assessor 6776000110 / 2322 BRYCE
CANYON CT, with PARKWOOD DIV 2 / DIV 3 lot-11s correctly excluded by lot suffix.
"""

from src.scrapers.enrichment.pierce_legal_repair import (
    collect_legal_matches,
    parse_pierce_legal,
    same_lot_suffix,
)


def _feat(parcel, legal, site="1 MAIN ST", city="PUYALLUP", zp="98374"):
    return {"attributes": {
        "TaxParcelNumber": parcel, "Legal_Description": legal,
        "Site_Address": site, "Delivery_Address": site,
        "City_State": f"{city}, WA", "Zipcode": zp,
    }}


class TestParsePierceLegal:
    def test_simple_plat_lot(self):
        assert parse_pierce_legal("PARKWOOD LT 11 (+)") == ("PARKWOOD", "11")
        assert parse_pierce_legal("PARKWOOD LT 11") == ("PARKWOOD", "11")
        assert parse_pierce_legal("PARKWOOD LOT 011") == ("PARKWOOD", "11")

    def test_rejects_unmodeled_qualifiers(self):
        for lg in ["PARKWOOD DIV 3 LT 11", "PARKWOOD LTS 11-12",
                   "PARKWOOD BLK 2 LT 11", "PARKWOOD ADD LT 11",
                   "PARKWOOD LT 11 LESS S 10FT",
                   "PARKWOOD LOT 11 / 12", "PARKWOOD LT 11, 12",
                   "PARKWOOD LOTS 11 TO 14"]:
            assert parse_pierce_legal(lg) is None, lg

    def test_rejects_no_lot_or_no_plat(self):
        assert parse_pierce_legal("SECTION 03 TWP 19 RANGE 04") is None
        assert parse_pierce_legal("L 11") is None          # no plat name
        assert parse_pierce_legal(None) is None
        assert parse_pierce_legal("") is None

    def test_rejects_like_metacharacters(self):
        assert parse_pierce_legal("PARK%WOOD LT 11") is None
        assert parse_pierce_legal("PARK_WOOD LT 11") is None


class TestSameLotSuffix:
    def test_prefix_typo_only(self):
        assert same_lot_suffix("6779000110", "6776000110") is True

    def test_different_lot_suffix_rejected(self):
        assert same_lot_suffix("6779000110", "6776020110") is False  # DIV 2
        assert same_lot_suffix("6779000110", "6776000120") is False  # lot 12

    def test_identical_or_nonten_digit_rejected(self):
        assert same_lot_suffix("6776000110", "6776000110") is False  # same prefix
        assert same_lot_suffix("123", "6776000110") is False
        assert same_lot_suffix(None, "6776000110") is False


class TestCollectLegalMatches:
    def test_hanson_three_divisions_all_collected_then_suffix_picks_one(self):
        feats = [
            _feat("6776000110", "SECTION 03 QUARTER 12 PARKWOOD L 11 EASE OF RECORD",
                  site="2322 BRYCE CANYON CT"),
            _feat("6776020110", "SECTION 03 QUARTER 12 PARKWOOD DIVISION # 2 L 11 EASE",
                  site="2611 GLACIER CT"),
            _feat("6776030110", "SECTION 03 QUARTER 14 PARKWOOD DIV. #3 L 11 EASE",
                  site="2804 14TH ST SE"),
        ]
        matches = collect_legal_matches(feats, "PARKWOOD", "11")
        assert {m["parcel_id"] for m in matches} == {"6776000110", "6776020110", "6776030110"}
        # The caller keeps only the one whose suffix matches the scraped parcel.
        picked = [m for m in matches if same_lot_suffix("6779000110", m["parcel_id"])]
        assert len(picked) == 1
        assert picked[0]["parcel_id"] == "6776000110"
        assert picked[0]["property_address"] == "2322 BRYCE CANYON CT"

    def test_excludes_wrong_lot_range_and_addressless(self):
        feats = [
            _feat("6776000100", "PARKWOOD L 10 EASE", site="2320 BRYCE CANYON CT"),
            _feat("6776001100", "PARKWOOD L 110 EASE", site="9 OTHER ST"),
            _feat("6776009911", "PARKWOOD L 11-12 EASE", site="9 RANGE ST"),
            _feat("6776009912", "PARKWOOD L 11 / 12 EASE", site="9 SLASH ST"),
            _feat("6776000110", "PARKWOOD L 11 EASE", site=""),  # no situs
        ]
        assert collect_legal_matches(feats, "PARKWOOD", "11") == []
