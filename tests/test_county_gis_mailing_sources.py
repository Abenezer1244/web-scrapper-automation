"""Mailing-address capture for the counties that previously had no source.

Snohomish and Cowlitz leads carried a property address and a NULL mailing address
100% of the time (measured in production 2026-09-10) because neither county was in
``_KNOWN_GIS_ENDPOINTS``: both fell through to the WA statewide layer, which is
situs-only and hardcodes ``mailing_address=None``.

The ArcGIS payloads below are real responses from the two county services, trimmed
to the configured ``out_fields``. They are fixtures of a live contract, not invented
data: every value here was returned by the county for the parcel it names.
"""
from src.scrapers.enrichment.county_gis import (
    _KNOWN_GIS_ENDPOINTS,
    _callers_for,
    _map_county_features,
    _parse_gis_response,
)

SNOHOMISH = _KNOWN_GIS_ENDPOINTS["snohomish_WA"]
COWLITZ = _KNOWN_GIS_ENDPOINTS["cowlitz_WA"]
DOUGLAS = _KNOWN_GIS_ENDPOINTS["douglas_WA"]


def _feature(attrs: dict) -> dict:
    return {"features": [{"attributes": attrs}]}


# ─── Snohomish ───────────────────────────────────────────────────────────────

def test_snohomish_owner_occupied_mailing_is_read_from_the_taxpayer_block():
    """The screenshot lead. Mailing equals the situs here because the COUNTY says
    the tax bill goes there, not because anything copied the property address."""
    parsed = _parse_gis_response(_feature({
        "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
        "situscity": "MOUNTLAKE TERRACE", "situsstate": "WA", "situszip": "98043-2946",
        "taxprline1": "22801 64TH PL W", "taxprcity": "MOUNTLAKE TERRACE",
        "taxprstate": "WA", "taxprzip": "98043",
    }), SNOHOMISH)

    assert parsed["property_address"] == "22801 64TH PL W"
    assert parsed["mailing_address"] == "22801 64TH PL W, MOUNTLAKE TERRACE, WA 98043"


def test_snohomish_absentee_owner_mailing_differs_from_the_property():
    """The signal the product actually sells: an out-of-state owner."""
    parsed = _parse_gis_response(_feature({
        "parcel_id": "00371200000100", "situsline1": "15009 72ND AVE W",
        "situscity": "EDMONDS", "situsstate": "WA", "situszip": "98026-4010",
        "taxprline1": "6191 SUNSET CREST WAY", "taxprcity": "SAN DIEGO",
        "taxprstate": "CA", "taxprzip": "98121",
    }), SNOHOMISH)

    assert parsed["property_address"] == "15009 72ND AVE W"
    assert parsed["mailing_address"] == "6191 SUNSET CREST WAY, SAN DIEGO, CA 98121"
    assert parsed["property_state"] == "WA"


def test_snohomish_po_box_mailing_is_kept():
    parsed = _parse_gis_response(_feature({
        "parcel_id": "00371400000202", "situsline1": "111 MADISON ST",
        "situscity": "EVERETT", "situsstate": "WA", "situszip": "98203-4935",
        "taxprline1": "PO BOX 4900", "taxprcity": "SCOTTSDALE",
        "taxprstate": "AZ", "taxprzip": "85261",
    }), SNOHOMISH)

    assert parsed["mailing_address"] == "PO BOX 4900, SCOTTSDALE, AZ 85261"


def test_snohomish_missing_taxpayer_street_stays_null():
    """4,921 parcels publish no taxprline1. A locality with no street is not an
    address, and must never be stored as though the lookup succeeded."""
    parsed = _parse_gis_response(_feature({
        "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
        "situscity": "MOUNTLAKE TERRACE", "situsstate": "WA", "situszip": "98043",
        "taxprline1": "", "taxprcity": "EVERETT", "taxprstate": "WA",
        "taxprzip": "98203",
    }), SNOHOMISH)

    assert parsed["property_address"] == "22801 64TH PL W"
    assert parsed["mailing_address"] is None


def test_snohomish_property_address_stays_street_only():
    """Adding a mailing source must not reformat property_address. These leads get
    a street-only property_address plus separate city/state/zip today."""
    parsed = _parse_gis_response(_feature({
        "parcel_id": "00583100000300", "situsline1": "1302 CASCADE DR UNIT 3",
        "situscity": "EVERETT", "situsstate": "WA", "situszip": "98203-6531",
        "taxprline1": "1302 CASCADE DR UNIT 3", "taxprcity": "EVERETT",
        "taxprstate": "WA", "taxprzip": "98203",
    }), SNOHOMISH)

    assert parsed["property_address"] == "1302 CASCADE DR UNIT 3"
    assert "EVERETT" not in parsed["property_address"]
    assert parsed["property_city"] == "EVERETT"
    assert parsed["property_state"] == "WA"
    assert parsed["property_zip"] == "98203-6531"


# ─── Cowlitz ─────────────────────────────────────────────────────────────────

def test_cowlitz_situs_is_composed_from_its_five_columns():
    parsed = _parse_gis_response(_feature({
        "PARCNO": "08931001", "SITUS_STREET_NUMBER": "3738",
        "SITUS_STREET_DIRECTION": "", "SITUS_STREET_NAME": "PENNSYLVANIA",
        "SITUS_STREET_SUFFIX": "ST", "SITUS_STREET_UNIT": "",
        "SITUS_CITY": "LONGVIEW", "SITUS_ZIP_CODE": "98632",
        "DEED_HOLDER_ADDRESS_1": "", "DEED_HOLDER_ADDRESS_2": "3738 PENNSYLVANIA ST",
        "DEED_HOLDER_CITY": "LONGVIEW", "DEED_HOLDER_STATE": "WA",
        "DEED_HOLDER_ZIPCODE": "98632",
    }), COWLITZ)

    # A null middle component must not leave a double space.
    assert parsed["property_address"] == "3738 PENNSYLVANIA ST"
    assert parsed["mailing_address"] == "3738 PENNSYLVANIA ST, LONGVIEW, WA 98632"
    assert parsed["property_state"] == "WA"


def test_cowlitz_attn_line_is_skipped_and_the_real_street_used():
    """DEED_HOLDER_ADDRESS_1 is an addressee line on 4,473 parcels. Taking it would
    corrupt the address AND store a taxpayer name, which this codebase does not
    collect."""
    parsed = _parse_gis_response(_feature({
        "PARCNO": "90293", "SITUS_STREET_NUMBER": "", "SITUS_STREET_DIRECTION": "",
        "SITUS_STREET_NAME": "", "SITUS_STREET_SUFFIX": "", "SITUS_STREET_UNIT": "",
        "SITUS_CITY": "", "SITUS_ZIP_CODE": "",
        "DEED_HOLDER_ADDRESS_1": "ATTN ALAN M ANNIS, DIRECTOR OF TAXES",
        "DEED_HOLDER_ADDRESS_2": "PO BOX 961089",
        "DEED_HOLDER_CITY": "FORT WORTH", "DEED_HOLDER_STATE": "TX",
        "DEED_HOLDER_ZIPCODE": "76161-0089",
    }), COWLITZ)

    assert parsed["mailing_address"] == "PO BOX 961089, FORT WORTH, TX 76161-0089"
    assert "ANNIS" not in (parsed["mailing_address"] or "")


def test_cowlitz_care_of_name_with_no_street_yields_null_not_a_name():
    parsed = _parse_gis_response(_feature({
        "PARCNO": "EM1101001", "SITUS_STREET_NUMBER": "", "SITUS_STREET_DIRECTION": "",
        "SITUS_STREET_NAME": "", "SITUS_STREET_SUFFIX": "", "SITUS_STREET_UNIT": "",
        "SITUS_CITY": "", "SITUS_ZIP_CODE": "",
        "DEED_HOLDER_ADDRESS_1": "C/O WA ST DEPT OF NATURAL RESOURCES",
        "DEED_HOLDER_ADDRESS_2": "", "DEED_HOLDER_CITY": "CASTLE ROCK",
        "DEED_HOLDER_STATE": "WA", "DEED_HOLDER_ZIPCODE": "98611",
    }), COWLITZ)

    assert parsed["mailing_address"] is None
    # and no state asserted for a parcel nothing located
    assert parsed.get("property_city") is None


def test_cowlitz_street_only_in_line_1_is_still_used():
    """203 parcels put the street in _1 with _2 empty."""
    parsed = _parse_gis_response(_feature({
        "PARCNO": "628350100", "SITUS_STREET_NUMBER": "125",
        "SITUS_STREET_DIRECTION": "", "SITUS_STREET_NAME": "MISSION",
        "SITUS_STREET_SUFFIX": "RD", "SITUS_STREET_UNIT": "A",
        "SITUS_CITY": "KELSO", "SITUS_ZIP_CODE": "98626",
        "DEED_HOLDER_ADDRESS_1": "229 CARROLL WAY", "DEED_HOLDER_ADDRESS_2": "",
        "DEED_HOLDER_CITY": "CHEHALIS", "DEED_HOLDER_STATE": "WA",
        "DEED_HOLDER_ZIPCODE": "98532-9155",
    }), COWLITZ)

    assert parsed["property_address"] == "125 MISSION RD A"
    assert parsed["mailing_address"] == "229 CARROLL WAY, CHEHALIS, WA 98532-9155"


# ─── No fabrication ──────────────────────────────────────────────────────────

def test_property_address_is_never_echoed_into_mailing():
    """A layer with situs but no taxpayer block must leave mailing NULL."""
    parsed = _parse_gis_response(_feature({
        "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
        "situscity": "MOUNTLAKE TERRACE", "situsstate": "WA", "situszip": "98043",
    }), SNOHOMISH)

    assert parsed["property_address"] == "22801 64TH PL W"
    assert parsed["mailing_address"] is None


# ─── Response-id validation ──────────────────────────────────────────────────

def test_leading_zero_coerced_parcel_still_maps_to_its_caller():
    """A layer typed numeric can echo 8931001 for the 08931001 we asked about."""
    assert _callers_for(8931001, {"08931001": ["08931001"]}) == ["08931001"]


def test_unrequested_parcel_is_dropped_not_invented():
    """Filing a real owner's mailing address under an id nobody asked for is worse
    than returning nothing."""
    assert _callers_for("99999999", {"08931001": ["08931001"]}) == []


def test_county_row_with_mailing_but_no_situs_is_kept_for_statewide_topup():
    """Previously dropped outright, which threw the mailing address away and let the
    situs-only statewide layer answer instead."""
    mapped = _map_county_features(
        [{"attributes": {
            "PARCNO": "90293", "SITUS_STREET_NUMBER": "", "SITUS_STREET_DIRECTION": "",
            "SITUS_STREET_NAME": "", "SITUS_STREET_SUFFIX": "", "SITUS_STREET_UNIT": "",
            "SITUS_CITY": "", "SITUS_ZIP_CODE": "",
            "DEED_HOLDER_ADDRESS_1": "", "DEED_HOLDER_ADDRESS_2": "PO BOX 961089",
            "DEED_HOLDER_CITY": "FORT WORTH", "DEED_HOLDER_STATE": "TX",
            "DEED_HOLDER_ZIPCODE": "76161-0089",
        }}],
        COWLITZ,
        {"90293": ["90293"]},
    )

    assert mapped["90293"]["mailing_address"] == "PO BOX 961089, FORT WORTH, TX 76161-0089"
    assert mapped["90293"]["needs_situs_fallback"] is True


def test_county_row_with_neither_situs_nor_mailing_is_still_dropped():
    """Unchanged behaviour: nothing to keep, so statewide should get the parcel."""
    mapped = _map_county_features(
        [{"attributes": {
            "PARCNO": "EM1101001", "SITUS_STREET_NUMBER": "",
            "SITUS_STREET_DIRECTION": "", "SITUS_STREET_NAME": "",
            "SITUS_STREET_SUFFIX": "", "SITUS_STREET_UNIT": "",
            "SITUS_CITY": "", "SITUS_ZIP_CODE": "",
            "DEED_HOLDER_ADDRESS_1": "C/O WA ST DNR", "DEED_HOLDER_ADDRESS_2": "",
            "DEED_HOLDER_CITY": "CASTLE ROCK", "DEED_HOLDER_STATE": "WA",
            "DEED_HOLDER_ZIPCODE": "98611",
        }}],
        COWLITZ,
        {"EM1101001": ["EM1101001"]},
    )

    assert mapped == {}


# ─── Addressee prefixes glued onto a real street ─────────────────────────────

def _sno_mail(line1: str) -> str | None:
    return _parse_gis_response(_feature({
        "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
        "situscity": "EVERETT", "situsstate": "WA", "situszip": "98203",
        "taxprline1": line1, "taxprcity": "SEATTLE", "taxprstate": "WA",
        "taxprzip": "98133",
    }), SNOHOMISH)["mailing_address"]


def test_care_of_prefix_is_stripped_and_the_street_kept():
    assert _sno_mail("C/O RYAN LLC 10500 NE 8TH ST SUITE 1400") == (
        "10500 NE 8TH ST SUITE 1400, SEATTLE, WA 98133")


def test_attn_prefix_with_a_person_name_does_not_store_the_name():
    got = _sno_mail("ATTN SUSAN CORNELL 981 POWELL AVE SW")
    assert got == "981 POWELL AVE SW, SEATTLE, WA 98133"
    assert "CORNELL" not in got


def test_stripping_an_addressee_must_not_eat_the_po_box():
    """Cutting at the first DIGIT turned this into a bare '330310'."""
    assert _sno_mail("DEPT OF TRANS PO BOX 330310") == "PO BOX 330310, SEATTLE, WA 98133"
    assert _sno_mail("PROP TAX MRG - R MASCHING PO BOX 152206") == (
        "PO BOX 152206, SEATTLE, WA 98133")


def test_a_street_that_does_not_start_with_a_number_is_kept():
    """taxprline1 is Snohomish's only mailing column, so there is nothing to
    disambiguate and a house-number rule would just discard real addresses."""
    assert _sno_mail("ONE ASHLEY WAY") == "ONE ASHLEY WAY, SEATTLE, WA 98133"


def test_unit_first_and_foreign_streets_are_kept():
    assert _sno_mail("#2011 7495 132ND ST") == "#2011 7495 132ND ST, SEATTLE, WA 98133"
    assert _sno_mail("67-6588 SOUTHOAKS CR") == "67-6588 SOUTHOAKS CR, SEATTLE, WA 98133"


def test_placeholder_tokens_are_not_addresses():
    for token in ("UNKNOWN", "NONE", "N/A", "null"):
        assert _sno_mail(token) is None


# ─── Ambiguous parcel ids ────────────────────────────────────────────────────

def test_two_parcels_sharing_a_loose_key_are_never_guessed_between():
    """'0123456' and '123456' are different parcels. Picking either would put one
    owner's mailing address on the other's property."""
    assert _callers_for("123456", {"0123456": ["0123456"], "123456": ["123456"]}) == [
        "123456"]  # exact match still wins
    assert _callers_for("00123456", {"0123456": ["0123456"], "123456": ["123456"]}) == []


# ─── Codex gate on the Phase 1 diff (2026-09-10) ─────────────────────────────
#
# Only the network boundary is replaced below (the county/statewide HTTP call, as
# test_county_gis_parse.TestFallbackOrder already does). The mapping, id resolution
# and merge logic under test all run for real.

class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_statewide_unrequested_parcel_is_dropped_not_filed_under_itself(monkeypatch):
    """P2. The statewide path used query_to_original.get(pid, pid), the same
    invent-a-caller pattern the county path dropped."""
    from src.scrapers.enrichment import county_gis as cg

    monkeypatch.setattr(cg, "safe_get", lambda *a, **kw: _Resp({"features": [
        {"attributes": {"ORIG_PARCEL_ID": "5555555", "SITUS_ADDRESS": "1 OTHER ST"}},
    ]}))
    assert cg._batch_query_wa_statewide(["2231502"], "cowlitz") == {}


def test_statewide_leading_zero_echo_still_reaches_its_caller(monkeypatch):
    from src.scrapers.enrichment import county_gis as cg

    monkeypatch.setattr(cg, "safe_get", lambda *a, **kw: _Resp({"features": [
        {"attributes": {"ORIG_PARCEL_ID": "8931001", "SITUS_ADDRESS": "3738 PENNSYLVANIA ST",
                        "SITUS_CITY_NM": "LONGVIEW", "SITUS_ZIP_NR": "98632"}},
    ]}))
    out = cg._batch_query_wa_statewide(["08931001"], "cowlitz")
    assert list(out) == ["08931001"]
    assert out["08931001"]["property_address"] == "3738 PENNSYLVANIA ST"
    assert out["08931001"]["mailing_address"] is None


def test_arcgis_error_inside_a_200_is_logged_not_read_as_no_data(monkeypatch, caplog):
    """A renamed field or a newly required token comes back as HTTP 200 + error body,
    which used to look exactly like a county with no mailing data."""
    from src.scrapers.enrichment import county_gis as cg

    monkeypatch.setattr(cg, "safe_get", lambda *a, **kw: _Resp(
        {"error": {"code": 499, "message": "Token Required"}}))
    with caplog.at_level("WARNING"):
        assert cg._batch_query_county(["00522400008900"], SNOHOMISH) == {}
    assert "Token Required" in caplog.text


def test_truncated_arcgis_page_is_logged(caplog):
    from src.scrapers.enrichment import county_gis as cg

    with caplog.at_level("WARNING"):
        cg._warn_on_arcgis_anomaly({"features": [], "exceededTransferLimit": True}, "x")
    assert "exceededTransferLimit" in caplog.text


class TestSingleParcelKeepsCountyMailing:
    """P1. enrich_parcel_gis asked the county, got a mailing address but no situs, then
    returned a situs-only fallback wholesale and lost the mailing address."""

    MAIL = "PO BOX 961089, FORT WORTH, TX 76161-0089"

    def _patch(self, monkeypatch, statewide, statewide_name=None):
        from src.scrapers.enrichment import county_gis as cg

        monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
        monkeypatch.setattr(cg, "_query_gis", lambda *a, **kw: {
            **cg._empty(), "matched": True, "mailing_address": self.MAIL})
        monkeypatch.setattr(cg, "_query_wa_statewide", lambda *a, **kw: statewide)
        monkeypatch.setattr(cg, "_query_gis_by_name", lambda *a, **kw: cg._empty())
        monkeypatch.setattr(cg, "_query_wa_statewide_by_name",
                            lambda *a, **kw: statewide_name or cg._empty())
        return cg

    def test_statewide_situs_is_topped_up_with_the_county_mailing(self, monkeypatch):
        cg = self._patch(monkeypatch, {"property_address": "1302 CASCADE DR",
                                       "mailing_address": None})
        out = cg.enrich_parcel_gis("90293", "cowlitz", "WA")
        assert out["property_address"] == "1302 CASCADE DR"
        assert out["mailing_address"] == self.MAIL

    def test_nothing_located_still_returns_the_county_mailing(self, monkeypatch):
        cg = self._patch(monkeypatch, cg_empty := {"property_address": None,
                                                   "mailing_address": None})
        out = cg.enrich_parcel_gis("90293", "cowlitz", "WA")
        assert out["property_address"] is None
        assert out["mailing_address"] == self.MAIL
        assert cg_empty["mailing_address"] is None

    def test_mailing_is_never_attached_to_a_name_search_hit(self, monkeypatch):
        """A name search may land on a DIFFERENT parcel. This parcel's mailing address
        must not ride along onto it."""
        cg = self._patch(
            monkeypatch,
            {"property_address": None, "mailing_address": None},
            statewide_name={"property_address": "999 SOMEONE ELSES RD",
                            "mailing_address": None},
        )
        out = cg.enrich_parcel_gis(None, "cowlitz", "WA", owner_name="FOYEN VERLINDA")
        assert out["mailing_address"] is None


# ─── Douglas (2026-10-03) ────────────────────────────────────────────────────
# Unlike the sections above, these VALUES are synthetic: the columns, their shapes and
# the Address2-then-Address1 layout are the live layer's (verified on a 2,000-row
# sample), but no real taxpayer's name or address is committed.

def _douglas(**attrs) -> dict:
    row = {"ParcelNumb": "00000000001", "Situs": "1 SAMPLE RD", "Address1": None,
           "Address2": None, "City": None, "State": None, "Zip": None}
    return _parse_gis_response(_feature({**row, **attrs}), DOUGLAS)


def test_douglas_mailing_street_is_address2_and_an_addressee_line_is_ignored():
    parsed = _douglas(Address1="C/O SAMPLE TRUSTEE", Address2="PO BOX 5",
                      City="WATERVILLE", State="WA", Zip="98858")
    assert parsed["mailing_address"] == "PO BOX 5, WATERVILLE, WA 98858"
    assert parsed["property_address"] == "1 SAMPLE RD"


def test_douglas_falls_back_to_address1_when_it_alone_is_a_street():
    parsed = _douglas(Address1="22 OTHER ST", City="BOISE", State="ID", Zip="83702")
    assert parsed["mailing_address"] == "22 OTHER ST, BOISE, ID 83702"


def test_douglas_situs_is_in_wa_so_an_out_of_state_owner_is_computable():
    from src.utils.address_intel import compute_owner_flags

    parsed = _douglas(Address2="22 OTHER ST", City="BOISE", State="ID", Zip="83702")
    assert parsed.get("property_state") == "WA"
    flags = compute_owner_flags(parsed["property_address"], parsed["mailing_address"],
                                property_state=parsed["property_state"])
    assert flags["out_of_state_owner"] is True


def test_douglas_publishes_the_postcode_as_is_never_zero_padded():
    parsed = _douglas(Address2="7 HARBOUR ST", City="SYDNEY", State="AUSTRALIA", Zip="2000")
    assert parsed["mailing_address"] == "7 HARBOUR ST, SYDNEY, AUSTRALIA 2000"


def test_a_placeholder_plus_four_is_dropped_and_a_real_one_kept():
    assert _douglas(Address2="1 A ST", City="EAST WENATCHEE", State="WA",
                    Zip="98802-0000")["mailing_address"] == "1 A ST, EAST WENATCHEE, WA 98802"
    assert _douglas(Address2="1 A ST", City="EAST WENATCHEE", State="WA",
                    Zip="98802-1234")["mailing_address"] == "1 A ST, EAST WENATCHEE, WA 98802-1234"
    # Only a US ZIP+4 is touched: any other value ending in -0000 is left as published.
    assert _douglas(Address2="1 A ST", City="X", State="AUSTRALIA",
                    Zip="AB-0000")["mailing_address"] == "1 A ST, X, AUSTRALIA AB-0000"


def test_douglas_with_no_mailing_street_stores_no_mailing():
    assert _douglas(City="WATERVILLE", State="WA", Zip="98858")["mailing_address"] is None


# ─── Codex gate on the rebased branch (2026-09-12) ───────────────────────────

def test_a_dashed_echo_resolves_to_the_dashless_request():
    assert _callers_for("602543-087-0", {"6025430870": ["602543-087-0"]}) == ["602543-087-0"]


def test_two_dashed_spellings_of_one_callers_parcel_are_not_ambiguous():
    """Kitsap's statewide query sends two dashed formats for ONE caller parcel."""
    keys = {"012302-2-005-2007": ["01230220052007"], "0123-022-005-2007": ["01230220052007"]}
    assert _callers_for("12302220052007", keys) == []  # different digits: no match
    assert _callers_for("01230220052007", keys) == ["01230220052007"]


def test_a_dash_insensitive_match_across_different_parcels_still_drops():
    keys = {"0123456": ["0123456"], "12-3456": ["12-3456"]}
    assert _callers_for("123456", keys) == []


def test_statewide_top_up_keeps_the_county_situs_parts_it_lacks(monkeypatch):
    from src.scrapers.enrichment import county_gis as cg

    mail = "PO BOX 961089, FORT WORTH, TX 76161-0089"
    monkeypatch.setattr(cg, "_batch_query_county", lambda pids, cfg, **kw: {
        "08931001": {"property_address": None, "mailing_address": mail,
                     "property_city": "LONGVIEW", "property_state": "WA",
                     "property_zip": "98632", "needs_situs_fallback": True},
    })
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", lambda pids, county: {
        "08931001": {"property_address": "3738 PENNSYLVANIA ST", "mailing_address": None,
                     "property_city": None, "property_state": "WA", "property_zip": None},
    })
    out = cg.batch_enrich_parcels_gis(["08931001"], "cowlitz", "WA")["08931001"]
    assert out["property_address"] == "3738 PENNSYLVANIA ST"
    assert out["mailing_address"] == mail
    assert (out["property_city"], out["property_zip"]) == ("LONGVIEW", "98632")
    assert "needs_situs_fallback" not in out


async def test_parcel_wrapper_keeps_a_mailing_only_gis_answer(monkeypatch):
    from src.scrapers.enrichment import county_gis as cg
    from src.scrapers.enrichment import parcel as p

    monkeypatch.setattr(p, "_source_down", {})
    monkeypatch.setattr(cg, "enrich_parcel_gis", lambda *a, **kw: {
        "property_address": None, "mailing_address": "22801 64TH PL W, MOUNTLAKE TERRACE, WA 98043"})
    from src.config import settings
    monkeypatch.setattr(settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
    out = await p.enrich_parcel("00522400008900", "snohomish", "WA")
    assert out["mailing_address"] == "22801 64TH PL W, MOUNTLAKE TERRACE, WA 98043"
    assert out["property_address"] is None
