"""Pierce probate legal-description parcel repair — pure-function guards.

No DB, no network (the HTTP paths are exercised live during development; these
tests lock the parsing/filtering/suffix logic that decides whether a repair is
safe). Regression guard for the live HANSON case: scraped parcel 6779000110
(nonexistent) + legal "PARKWOOD LT 11" -> assessor 6776000110 / 2322 BRYCE
CANYON CT, with PARKWOOD DIV 2 / DIV 3 lot-11s correctly excluded by lot suffix.
"""

from src.scrapers.enrichment.pierce_legal_repair import (
    collect_legal_matches,
    legal_plat_adjacent,
    parcel_repair_method,
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
        assert parse_pierce_legal("PARKWOOD LT 11 (+)") == ("PARKWOOD", "11", None)
        assert parse_pierce_legal("PARKWOOD LT 11") == ("PARKWOOD", "11", None)
        assert parse_pierce_legal("PARKWOOD LOT 011") == ("PARKWOOD", "11", None)

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


# ── 2026-09-02 (Test 2 audit): trailing block + single-digit recorder typos ──

class TestTrailingBlock:
    def test_trailing_block_is_parsed(self):
        assert parse_pierce_legal("RHODODENDRON LANES LT 6 BLK 3 (+)") == ("RHODODENDRON LANES", "6", "3")
        assert parse_pierce_legal("PALMER LAKE LT 28 BLK 5") == ("PALMER LAKE", "28", "5")
        assert parse_pierce_legal("PALMER LAKE LT 28 B 05") == ("PALMER LAKE", "28", "5")

    def test_block_elsewhere_or_range_still_rejected(self):
        assert parse_pierce_legal("PARKWOOD BLK 2 LT 11") is None
        assert parse_pierce_legal("PARKWOOD LT 11 BLK 2-3") is None
        assert parse_pierce_legal("PARKWOOD LTS 11-12 BLK 2") is None

    def test_block_token_must_match_exactly_and_bounded(self):
        feats = [
            _feat("7185000190", "SECTION 11 QUARTER 24 RHODODENDRON LANES L 6 B 3 SUBJ TO EASE", site="6117 119TH ST SW"),
            _feat("7185000100", "SECTION 11 QUARTER 24 RHODODENDRON LANES: RHODODENDRON LANES L 6 B 2", site="6102 119TH ST SW"),
            _feat("7185000330", "RHODODENDRON LANES L 6 B 30", site="1 THIRTY ST"),
            _feat("7185000340", "RHODODENDRON LANES L 6 B 3-4", site="1 RANGE ST"),
        ]
        matches = collect_legal_matches(feats, "RHODODENDRON LANES", "6", "3")
        assert [m["parcel_id"] for m in matches] == ["7185000190"]

    def test_no_block_in_scrape_keeps_every_block_candidate(self):
        # Without a scraped block the caller sees BOTH -> ambiguous -> no repair.
        feats = [
            _feat("7185000190", "RHODODENDRON LANES L 6 B 3", site="6117 119TH ST SW"),
            _feat("7185000100", "RHODODENDRON LANES L 6 B 2", site="6102 119TH ST SW"),
        ]
        assert len(collect_legal_matches(feats, "RHODODENDRON LANES", "6")) == 2


class TestParcelRepairMethod:
    def test_suffix_class_wins(self):
        assert parcel_repair_method("6779000110", "6776000110") == "plat_lot_unique_suffix"

    def test_single_substituted_digit(self):
        # live KOENIG row: THORSON RIDGE LT 5
        assert parcel_repair_method("9066600050", "9066000050") == "plat_lot_unique_edit1"

    def test_single_dropped_digit(self):
        # live MCPHERSON row: RHODODENDRON LANES LT 6 BLK 3 (9-digit scrape)
        assert parcel_repair_method("718500090", "7185000190") == "plat_lot_unique_edit1"

    def test_two_edits_identical_or_bad_shapes_rejected(self):
        assert parcel_repair_method("9066600051", "9066000050") is None   # 2 edits
        assert parcel_repair_method("9066000050", "9066000050") is None   # identical
        assert parcel_repair_method("12345", "9066000050") is None        # too short
        assert parcel_repair_method("9066600050", "906600005") is None    # GIS side not 10 digits
        assert parcel_repair_method(None, "9066000050") is None


class TestLegalPlatAdjacent:
    """The edit-distance-1 path has no lot-suffix anchor, so the GIS legal must name
    the plat IMMEDIATELY before the lot — a division qualifier in between is a
    different subdivision (Codex review)."""

    def test_live_shapes_pass(self):
        assert legal_plat_adjacent(
            "SECTION 06 TOWNSHIP 18 RANGE 04 QUARTER 32 THORSON RIDGE: THORSON RIDGE L 5 TOG/W 1/34 INT",
            "THORSON RIDGE", "5")
        assert legal_plat_adjacent(
            "SECTION 11  TOWNSHIP 19  RANGE 02  QUARTER 24   RHODODENDRON LANES  L 6 B 3 SUBJ TO EASE",
            "RHODODENDRON LANES", "6", "3")

    def test_division_between_plat_and_lot_fails(self):
        assert not legal_plat_adjacent("SECTION 03 QUARTER 12 PARKWOOD DIVISION # 2 L 11 EASE", "PARKWOOD", "11")
        assert not legal_plat_adjacent("PARKWOOD DIV. #3 L 11 EASE", "PARKWOOD", "11")

    def test_wrong_lot_block_or_range_fails(self):
        assert not legal_plat_adjacent("THORSON RIDGE L 50", "THORSON RIDGE", "5")
        assert not legal_plat_adjacent("THORSON RIDGE L 5-6", "THORSON RIDGE", "5")
        assert not legal_plat_adjacent("RHODODENDRON LANES L 6 B 2", "RHODODENDRON LANES", "6", "3")
        assert not legal_plat_adjacent(None, "THORSON RIDGE", "5")
