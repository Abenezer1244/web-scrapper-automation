"""A county layer that answers with NO attributes is degraded, not authoritative.

Snohomish stripped every attribute column off its public parcel layer between
2026-09-14 and 2026-09-18 while still returning HTTP 200 and still matching on
parcel_id: 0 of 319,733 rows kept a non-null situsline1/ownername/taxprname.
Mailing coverage for snohomish/pre_foreclosure went 46/49 -> 0/3 in one run and
NOTHING signalled it. The identifier-only feature was dropped by the mapper, the
statewide layer supplied the property address, and the absence of a transport
error was read as "this parcel has no mailing address" — permanently, because
only `county_unreached` rows are ever revisited by background recovery.

These pin the contract: matched + no payload = INDETERMINATE -> deferred.
Pure, no network.
"""
from src.scrapers.enrichment.county_gis import (
    _KNOWN_GIS_ENDPOINTS,
    _configured_payload_fields,
    _feature_payload_is_empty,
    _map_county_features,
)

SNOHOMISH = _KNOWN_GIS_ENDPOINTS["snohomish_WA"]
PIERCE = _KNOWN_GIS_ENDPOINTS["pierce_WA"]

# A real Snohomish parcel as the layer serves it TODAY: id present, everything else
# null. Captured live 2026-09-18.
_STRIPPED = {
    "parcel_id": "00437860401300",
    "situsline1": None, "situscity": None, "situsstate": None, "situszip": None,
    "taxprline1": None, "taxprcity": None, "taxprstate": None, "taxprzip": None,
}

# The same parcel as the layer served it on 2026-09-14.
_POPULATED = {
    "parcel_id": "00437860401300",
    "situsline1": "2407 EVERETT AVE", "situscity": "EVERETT",
    "situsstate": "WA", "situszip": "98201-3725",
    "taxprline1": "2407 EVERETT AVE", "taxprcity": "EVERETT",
    "taxprstate": "WA", "taxprzip": "98201",
}


class TestConfiguredPayloadFields:
    def test_collects_situs_and_mailing_columns(self):
        fields = _configured_payload_fields(SNOHOMISH)
        assert "situsline1" in fields
        assert "taxprline1" in fields
        assert "taxprcity" in fields

    def test_excludes_the_parcel_id_column(self):
        # An id is identity, not payload: it is present on every match, so counting
        # it would make the predicate permanently False.
        assert SNOHOMISH["parcel_field"] not in _configured_payload_fields(SNOHOMISH)

    def test_positional_none_placeholders_are_skipped(self):
        # Cowlitz declares situs_part_fields ["SITUS_CITY", None, "SITUS_ZIP_CODE"]:
        # the None position names no column.
        cfg = {"situs_part_fields": ["SITUS_CITY", None, "SITUS_ZIP_CODE"]}
        assert _configured_payload_fields(cfg) == ["SITUS_CITY", "SITUS_ZIP_CODE"]


class TestFeaturePayloadIsEmpty:
    def test_stripped_feature_is_empty(self):
        assert _feature_payload_is_empty(_STRIPPED, SNOHOMISH) is True

    def test_populated_feature_is_not_empty(self):
        assert _feature_payload_is_empty(_POPULATED, SNOHOMISH) is False

    def test_blank_strings_count_as_empty(self):
        attrs = {**_STRIPPED, "situscity": "   ", "taxprzip": ""}
        assert _feature_payload_is_empty(attrs, SNOHOMISH) is True

    def test_vacant_land_with_a_real_mailing_address_is_NOT_empty(self):
        # The case the predicate must never misclassify: raw land with no situs at
        # all, but a taxpayer mailing address the county really does publish. That
        # is a genuine answer and must stay a genuine answer.
        attrs = {
            "parcel_id": "00437860401300",
            "situsline1": None, "situscity": None, "situsstate": None, "situszip": None,
            "taxprline1": "PO BOX 44", "taxprcity": "ZILLAH",
            "taxprstate": "WA", "taxprzip": "98953",
        }
        assert _feature_payload_is_empty(attrs, SNOHOMISH) is False

    def test_situs_only_answer_is_not_empty(self):
        attrs = {**_STRIPPED, "situsline1": "2407 EVERETT AVE"}
        assert _feature_payload_is_empty(attrs, SNOHOMISH) is False

    def test_config_reading_no_data_columns_is_never_empty(self):
        # Nothing was asked for, so nothing is missing.
        assert _feature_payload_is_empty({"PIN": "123"}, {"parcel_field": "PIN"}) is False


class TestMapperReportsDegraded:
    def test_stripped_feature_is_reported_degraded_and_not_returned(self):
        degraded: list[str] = []
        out = _map_county_features(
            [{"attributes": _STRIPPED}], SNOHOMISH,
            {"00437860401300": ["00437860401300"]}, degraded=degraded,
        )
        # Not an answer: the parcel still needs the statewide layer for its situs.
        assert out == {}
        # But it IS recorded, so the caller can defer the mailing lookup.
        assert degraded == ["00437860401300"]

    def test_populated_feature_is_returned_and_not_degraded(self):
        degraded: list[str] = []
        out = _map_county_features(
            [{"attributes": _POPULATED}], SNOHOMISH,
            {"00437860401300": ["00437860401300"]}, degraded=degraded,
        )
        assert degraded == []
        assert out["00437860401300"]["mailing_address"]

    def test_degraded_fans_out_to_every_caller_spelling(self):
        # One APN can arrive under several raw spellings in a batch; all of them
        # need the marker or the unmarked ones read as settled negatives.
        degraded: list[str] = []
        _map_county_features(
            [{"attributes": _STRIPPED}], SNOHOMISH,
            {"00437860401300": ["00437860401300", "0043786-0401300"]},
            degraded=degraded,
        )
        assert sorted(degraded) == ["0043786-0401300", "00437860401300"]

    def test_caller_that_passes_no_out_param_still_works(self):
        # degraded is optional; older callers must not break.
        assert _map_county_features(
            [{"attributes": _STRIPPED}], SNOHOMISH,
            {"00437860401300": ["00437860401300"]},
        ) == {}

    def test_king_vacant_parcel_is_NOT_degraded(self):
        # King declares mailing_fields=[] and skip_statewide_fallback. ~1/3 of its
        # delinquent parcels are vacant/raw land with a null ADDR_FULL and no
        # locality. Classifying those as degraded would DROP the feature instead of
        # returning matched + vacant_no_situs, losing the vacant marker the worker
        # persists and letting property recovery buy lookups for land with no
        # address to find (Codex). King must be untouched by this change.
        king = _KNOWN_GIS_ENDPOINTS["king_WA"]
        degraded: list[str] = []
        attrs = {
            "PIN": "3879900805", "ADDR_FULL": None,
            "POSTALCTYNAME": None, "STATE_ABBR": None, "ZIP5": None,
        }
        out = _map_county_features(
            [{"attributes": attrs}], king, {"3879900805": ["3879900805"]},
            degraded=degraded,
        )
        assert degraded == []
        assert out["3879900805"]["vacant_no_situs"] is True
        assert out["3879900805"]["matched"] is True

    def test_a_populated_sibling_feature_beats_an_empty_one(self):
        # One parcel can carry SEVERAL features (condo units). If an empty sibling
        # left the id in `degraded` it would land in county_unreached, and
        # mailing_recovery EXCLUDES every unreached parcel, discarding the real
        # mailing address and rotating the row forever without consuming an attempt
        # (Codex High). A real answer always wins, in either feature order.
        for feats in (
            [{"attributes": _STRIPPED}, {"attributes": _POPULATED}],
            [{"attributes": _POPULATED}, {"attributes": _STRIPPED}],
        ):
            degraded: list[str] = []
            out = _map_county_features(
                feats, SNOHOMISH, {"00437860401300": ["00437860401300"]},
                degraded=degraded,
            )
            # The mapper itself may report both; the caller reconciles. Assert the
            # reconciliation rule the caller applies.
            reconciled = [pid for pid in degraded if pid not in out]
            assert reconciled == []
            assert out["00437860401300"]["mailing_address"]

    def test_pierce_empty_feature_is_degraded_too(self):
        # Not Snohomish-specific: any configured county layer that answers with no
        # payload is degraded. Pierce reads Delivery_Address/City_State/Zipcode.
        degraded: list[str] = []
        attrs = {
            "TaxParcelNumber": "0019012000", "Site_Address": None,
            "Delivery_Address": None, "City_State": None, "Zipcode": None,
            "Legal_Description": None,
        }
        out = _map_county_features(
            [{"attributes": attrs}], PIERCE, {"0019012000": ["0019012000"]},
            degraded=degraded,
        )
        assert out == {} and degraded == ["0019012000"]


class TestCompletionReporting:
    """The job must never claim mailing enrichment it did not achieve."""

    def test_success_only_when_nothing_is_missing(self):
        from src.workers.tasks_helpers.enrich import enrichment_completion_log

        level, msg = enrichment_completion_log({})
        assert level == "success" and "complete" in msg

    def test_rows_with_no_mailing_are_reported_even_when_nothing_deferred(self):
        # The exact 2026-09-18 shape: the county answered without erroring, so
        # mailing_deferred was 0 and the line read as full success while 3 of 3
        # leads had no mailing address.
        from src.workers.tasks_helpers.enrich import enrichment_completion_log

        level, msg = enrichment_completion_log({"mailing_missing": 3})
        assert level != "success"
        assert "3" in msg and "no mailing address available" in msg

    def test_singular_wording(self):
        from src.workers.tasks_helpers.enrich import enrichment_completion_log

        _, msg = enrichment_completion_log({"mailing_missing": 1})
        assert "1 lead has no mailing address available." in msg

    def test_deferred_wording_still_wins_when_both_present(self):
        # Deferred is the more actionable statement (recovery will retry), so it is
        # the one the line leads with; the missing count does not duplicate it.
        from src.workers.tasks_helpers.enrich import enrichment_completion_log

        _, msg = enrichment_completion_log({"mailing_deferred": 3, "mailing_missing": 3})
        assert "still pending" in msg
        assert "no mailing address available" not in msg
