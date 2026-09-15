"""King code violations resolved through King County's own address points.

The strict point rule (test_king_code_violation_semantics.py) leaves corner lots, lettered
townhomes and multi-address buildings unmatched because the parcel layer carries one
situs per polygon. These tests pin the address-point rule in
src/scrapers/enrichment/king_address_points.py and every read-side consequence.

Only external HTTP answers are substituted: the address-point and parcel-layer
attributes below are copied from live King GIS responses gathered 2026-09-14
(research_king_unmatched.md). Where a rule needs a shape no sampled address produced
(two PINs for one address, a PIN-by-id parcel answer) the payload is a real record with
the single field under test changed, and says so. Matching, gating, serialization,
export and DB writes run for real.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.api.schemas import ResultRow
from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_address_points as kap
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment import king_parcel_locate as kpl
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.utils.lead_export import build_lead_export_row
from src.utils.located_parcel import (
    located_parcel_id,
    located_parcel_match,
    mailing_lookup_pin,
    parcel_source_label,
)
from src.workers.property_identity import legacy_strong_signature

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_king_code_violation_owner.py"
_spec = importlib.util.spec_from_file_location("backfill_king_cv_owner_ap", _SCRIPT)
bko = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bko)


def _ap(pin, hn, num, full, compress, zip5, prim, filt, sitetype="R1", comments=""):
    return {"PIN": pin, "ADDR_HN": hn, "ADDR_NUM": num, "ADDR_FULL": full,
            "COMPRESS_NAME": compress, "ZIP5": zip5, "PRIM_ADDR": prim,
            "PRIM_ADDR_FILTER": filt, "SITETYPE": sitetype, "Unit": None, "Building": None,
            "CTYNAME": "Seattle", "COMMENTS": comments}


# ── Real King address points (KingCo_AddressPoints/MapServer/0) ────────────────
AP_9043A = _ap("7899800716", "9043 A", 9043, "9043A 18TH AVE SW", "18THAVESW", "98106", 1,
               "E911:ONETOONE", comments="Seattle short subdivision 3030782, added 10/23/18")
AP_9043B = _ap("7899800717", "9043 B", 9043, "9043B 18TH AVE SW", "18THAVESW", "98106", 1,
               "E911:ONETOONE", comments="Seattle short subdivision 3030782, added 10/23/18")
AP_2001_DRAVUS = _ap("2770602445", "2001", 2001, "2001 W DRAVUS ST", "WDRAVUSST", "98199", 0,
                     None, sitetype="C1", comments="ROMIO'S PIZZA AND PASTA")
AP_2605 = [_ap("2770600900", "2605 A", 2605, "2605A 22ND AVE W", "22NDAVEW", "98199", 1, "E911:ONETOONE"),
           _ap("2770600901", "2605 B", 2605, "2605B 22ND AVE W", "22NDAVEW", "98199", 1, "E911:ONETOONE"),
           _ap("2770600902", "2605 C", 2605, "2605C 22ND AVE W", "22NDAVEW", "98199", 1, "E911:ONETOONE"),
           _ap("2770600903", "2605 D", 2605, "2605D 22ND AVE W", "22NDAVEW", "98199", 1, "E911:ONETOONE"),
           _ap("2770600904", "2605 E", 2605, "2605E 22ND AVE W", "22NDAVEW", "98199", 1, "E911:ONETOONE")]
AP_3810_GALER = _ap("5318100580", "3810", 3810, "3810 E GALER ST", "EGALERST", "98112", 1,
                    "E911:ONETOONE", comments=" ")
AP_209_12TH = _ap("9822000330", "209", 209, "209 12TH AVE S", "12THAVES", "98144", 1,
                  "APTCOMPLEX_EXTR", sitetype="R2",
                  comments="Mason & Main Apartments, 10 stories, 335 units")
AP_1212_ALLEN = [_ap("7821200325", "1212 A", 1212, "1212A N ALLEN PL", "NALLENPL", "98103", 1,
                     "E911:ONETOONE", sitetype="R2"),
                 _ap("7821200326", "1212 B", 1212, "1212B N ALLEN PL", "NALLENPL", "98103", 1,
                     "E911:ONETOONE", sitetype="R2")]
AP_5452 = _ap("1773101445", "5452", 5452, "5452 25TH AVE SW", "25THAVESW", "98106", 1, "E911:ONETOONE")
AP_1765_22ND = _ap("1822300020", "1765", 1765, "1765 22ND AVE S", "22NDAVES", "98144", 1,
                   "E911:ONETOONE", sitetype="R2")
AP_4616_MLK = _ap("8562990000", "4616", 4616, "4616 MARTIN LUTHER KING JR WAY S",
                  "MARTINLUTHERKINGJRWAYS", "98108", 1, "CONDOCOMPLEX_EXTR", sitetype="R2",
                  comments="ALI fallout")
AP_3919_PASADENA = _ap(None, "3919", 3919, "3919 PASADENA PL NE", "PASADENAPLNE", "98105", 0, None,
                       sitetype="C1")
AP_821_WASHINGTON = _ap("9822000480", "821", 821, "821 S WASHINGTON ST", "SWASHINGTONST", "98104",
                        1, "E911:ONETOONE", sitetype="R2")

# ── Real parcel polygons under the SDCI point (KingCo_PropertyInfo/MapServer/2) ─
PARCEL_9043A = {"PIN": "7899800716", "ADDR_FULL": "9043A 18TH AVE SW", "ZIP5": "98106", "PROPTYPE": "R"}
PARCEL_2003_DRAVUS = {"PIN": "2770602445", "ADDR_FULL": "2003 W DRAVUS ST", "ZIP5": "98199", "PROPTYPE": "C"}
PARCEL_2605E = {"PIN": "2770600904", "ADDR_FULL": "2605E 22ND AVE W", "ZIP5": "98199", "PROPTYPE": "R"}
PARCELS_1765 = [{"PIN": "7548301095", "ADDR_FULL": None, "ZIP5": "98144", "PROPTYPE": "C"},
                {"PIN": "7548301115", "ADDR_FULL": None, "ZIP5": "98144", "PROPTYPE": "C"}]
PARCEL_4616_MLK = {"PIN": "8562990000", "ADDR_FULL": "4616 MARTIN LUTHER KING JR WAY S",
                   "ZIP5": "98108", "PROPTYPE": "K"}
PARCEL_1212A = {"PIN": "7821200325", "ADDR_FULL": "1212A N ALLEN PL", "ZIP5": "98103", "PROPTYPE": "R"}
# PIN-by-id answer for 5318100580: the layer's shape, PROPTYPE from its R1 address point.
PARCEL_5318100580 = {"PIN": "5318100580", "ADDR_FULL": "3810 E GALER ST", "ZIP5": "98112", "PROPTYPE": "R"}

# Real eRealProperty Detail.aspx markup for 7899800716 (cphContent_DetailsViewParcel).
PAGE_7899800716 = (
    '<tr class="GridViewRowStyle"><td style="font-weight:bold;width:200px;">Parcel</td>'
    '<td style="width:200px;">789980-0716</td></tr><tr class="GridViewAlternatingRowStyle">'
    '<td style="font-weight:bold;width:200px;">Name</td><td>NAMUE KATA &amp; ISABELLA MONGI      </td>'
    '</tr><tr class="GridViewRowStyle"><td style="font-weight:bold;width:200px;">Site Address</td>'
    '<td>9043 A 18TH AVE SW </td>')


class _Resp:
    def __init__(self, payload, status_code=200, text_body=""):
        self._payload, self.status_code, self.text = payload, status_code, text_body

    def json(self):
        return self._payload


class _Gis:
    """King GIS answering like the real layers: filters by the query it is sent."""

    def __init__(self, points=(), under_point=(), parcels=()):
        self.points, self.under_point, self.parcels = list(points), list(under_point), list(parcels)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        if url == kap.ADDRESS_POINT_LAYER:
            where = params["where"]
            num = int(re.search(r"ADDR_NUM=(\d+)", where).group(1))
            names = set(re.findall(r"'([A-Z0-9]+)'", where.split(" IN ", 1)[1]))
            assert "CTYNAME='Seattle'" in where
            feats = [p for p in self.points if p["ADDR_NUM"] == num and p["COMPRESS_NAME"] in names]
        elif "geometry" in params:
            feats = self.under_point
        else:
            pin = re.fullmatch(r"PIN='(\d{10})'", params["where"]).group(1)
            feats = [p for p in self.parcels if p["PIN"] == pin]
        return _Resp({"features": [{"attributes": a} for a in feats]})


def _gis(monkeypatch, **kw) -> _Gis:
    gis = _Gis(**kw)
    monkeypatch.setattr(kap, "safe_get", gis)
    monkeypatch.setattr(kpl, "safe_get", gis)
    monkeypatch.setattr(kap.time, "sleep", lambda s: None)
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    return gis


def _decide(lat, lon, address, **kw):
    return kap.match_address_point(lat, lon, address, pace_s=0, **kw)


# ── Address normalization ──────────────────────────────────────────────────────

def test_a_letter_after_the_number_is_read_both_ways():
    p = kap.parse_lead_address("2571 W MONTLAKE PL E")
    assert [(r.name, r.house, r.compress) for r in kap.readings(p)] == [
        ("unit_letter", "2571W", "MONTLAKEPLE"), ("directional", "2571", "WMONTLAKEPLE")]
    p = kap.parse_lead_address("9043 A 18TH AVE SW, SEATTLE WA 98106")
    assert (p.zip5, [(r.name, r.house) for r in kap.readings(p)]) == ("98106", [("unit_letter", "9043A")])
    assert kap.parse_lead_address("9043A 18th Avenue SW").letter == "A"
    assert kap.parse_lead_address("301 NE 103RD ST").letter is None


def test_king_street_abbreviations_are_spelled_as_king_writes_them():
    p = kap.parse_lead_address("4616 M L KING JR WAY S, SEATTLE WA 98108")
    assert (p.letter, p.street) == (None, "MARTIN LUTHER KING JR WAY S")
    assert kap.parse_lead_address("6007 MLK WAY S").street == "MARTIN LUTHER KING JR WAY S"


@pytest.mark.parametrize("address", ["4616 M L KING JR WAY S #6", "1212 N ALLEN PL UNIT B",
                                     "1750-1752 22ND AVE S", "123 1/2 MAIN ST", "", None,
                                     "ROOSEVELT WAY NE"])
def test_units_ranges_and_numberless_addresses_are_never_matched(address, monkeypatch):
    gis = _gis(monkeypatch, points=[AP_4616_MLK])
    assert kap.parse_lead_address(address) is None
    d = _decide("47.56", "-122.29", address)
    assert d.outcome == "rejected" and d.pin is None and gis.calls == []


# ── Accept rules ───────────────────────────────────────────────────────────────

def test_unit_letter_reading_on_the_parcel_under_the_point_is_an_address_point(monkeypatch):
    _gis(monkeypatch, points=[AP_9043A, AP_9043B], under_point=[PARCEL_9043A])
    d = _decide("47.52163912", "-122.35809767", "9043 A 18TH AVE SW, SEATTLE WA 98106")
    assert (d.outcome, d.pin, d.match) == ("accepted", "7899800716", "address_point")
    ev = d.evidence
    assert ev["letter_reading"] == "unit_letter" and ev["zip_compare"] == "equal"
    assert ev["coordinate_pins"] == ["7899800716"]
    assert ev["normalized_address"] == "9043 A 18TH AVE SW"


def test_directional_reading_finds_a_secondary_address_of_a_corner_lot(monkeypatch):
    gis = _gis(monkeypatch, points=[AP_2001_DRAVUS], under_point=[PARCEL_2003_DRAVUS])
    d = _decide("47.64827278", "-122.38223017", "2001 W DRAVUS ST, SEATTLE WA 98199")
    assert (d.outcome, d.pin, d.match) == ("accepted", "2770602445", "address_point")
    assert d.evidence["letter_reading"] == "directional"
    assert d.parcel_address == "2001 W DRAVUS ST"
    assert [u for u, _ in gis.calls] == [kap.ADDRESS_POINT_LAYER, kap.PARCEL_LAYER]


def test_e_is_a_unit_letter_when_king_says_so(monkeypatch):
    _gis(monkeypatch, points=AP_2605, under_point=[PARCEL_2605E])
    d = _decide("47.64319164", "-122.38484847", "2605 E 22ND AVE W, SEATTLE WA 98199")
    assert (d.outcome, d.pin, d.evidence["letter_reading"]) == ("accepted", "2770600904", "unit_letter")


def test_the_point_rules_parcels_are_reused_instead_of_asked_again(monkeypatch):
    gis = _gis(monkeypatch, points=[AP_9043A, AP_9043B])
    d = _decide("47.52", "-122.35", "9043 A 18TH AVE SW, SEATTLE WA 98106",
                point_parcels=(("7899800716", "R"),), point_status="address_mismatch")
    assert d.outcome == "accepted" and [u for u, _ in gis.calls] == [kap.ADDRESS_POINT_LAYER]
    assert d.evidence["point_status"] == "address_mismatch"


def test_no_coordinates_is_only_an_internal_address_only_candidate(monkeypatch):
    _gis(monkeypatch, points=[AP_3810_GALER], parcels=[PARCEL_5318100580])
    d = _decide(None, None, "3810 E GALER ST, SEATTLE WA 98112", point_status="no_coordinates")
    assert (d.outcome, d.pin, d.match) == ("accepted", "5318100580", "address_only")
    assert d.evidence["coordinate_pins"] is None
    ed = {**kap.decision_fields(d, checked_at="t"), "source": "seattle_sdci_code_violations"}
    assert ed["kc_pin_status"] == "matched" and ed["kc_pin_source"] == "king_gis_address_point"
    # Not shown, not named, no mailing.
    assert located_parcel_id(ed) is None and located_parcel_match(ed) is None
    assert parcel_source_label(ed) == "" and mailing_lookup_pin(ed) is None
    assert kpl.owner_lookup_pins([SimpleNamespace(party_name=None, enrichment_data=ed)]) == {}
    out = ResultRow(**_row(ed))
    assert (out.located_parcel_id, out.located_parcel_match) == (None, None)


# ── Reject rules ───────────────────────────────────────────────────────────────

def test_an_address_on_another_parcel_than_the_point_is_rejected(monkeypatch):
    # 1765 22ND AVE S: the address point's PIN is a land-only parcel next door.
    _gis(monkeypatch, points=[AP_1765_22ND], under_point=PARCELS_1765)
    d = _decide("47.58738280", "-122.30439161", "1765 22ND AVE S, SEATTLE WA 98144")
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "not_under_coordinates")
    assert d.evidence["coordinate_pins"] == ["7548301095", "7548301115"]
    assert kap.decision_fields(d, checked_at="t") == {"kc_address_point_evidence": {
        **d.evidence, "checked_at": "t"}}


def test_a_point_on_no_parcel_is_rejected(monkeypatch):
    _gis(monkeypatch, points=[AP_9043A])
    d = _decide("47.52", "-122.35", "9043 A 18TH AVE SW, SEATTLE WA 98106", point_parcels=())
    assert d.evidence["reason"] == "not_under_coordinates"


def test_a_zip_conflict_is_rejected(monkeypatch):
    _gis(monkeypatch, points=[AP_209_12TH])
    d = _decide("47.60068990", "-122.31756917", "209 12TH AVE S, SEATTLE WA 98104",
                point_parcels=(("9822000330", "C"),))
    assert (d.outcome, d.evidence["reason"], d.evidence["zip_compare"]) == (
        "rejected", "zip_conflict", "conflict")


def test_the_zip_column_counts_as_the_leads_zip(monkeypatch):
    _gis(monkeypatch, points=[AP_209_12TH])
    d = _decide("47.60", "-122.31", "209 12TH AVE S", point_parcels=(("9822000330", "C"),),
                property_zip="98104")
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "zip_conflict")
    d = _decide("47.60", "-122.31", "209 12TH AVE S, SEATTLE WA 98144",
                point_parcels=(("9822000330", "C"),), property_zip="98104")
    assert (d.evidence["reason"], d.evidence["zip_compare"]) == ("zip_conflict", "lead_zips_disagree")
    d = _decide("47.60", "-122.31", "209 12TH AVE S", point_parcels=(("9822000330", "C"),),
                property_zip="98144")
    assert (d.outcome, d.evidence["zip_compare"]) == ("accepted", "equal")
    d = _decide("47.60", "-122.31", "209 12TH AVE S", point_parcels=(("9822000330", "C"),),
                property_zip="98144X")
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "invalid_property_zip")


def test_a_lead_without_a_zip_is_compared_on_street_only(monkeypatch):
    _gis(monkeypatch, points=[AP_209_12TH])
    d = _decide("47.60", "-122.31", "209 12TH AVE S", point_parcels=(("9822000330", "C"),))
    assert (d.outcome, d.evidence["zip_compare"]) == ("accepted", "lead_has_no_zip")


def test_two_pins_for_one_address_are_rejected(monkeypatch):
    # AP_821_WASHINGTON as served, plus the same record on the building parcel next to it.
    _gis(monkeypatch, points=[AP_821_WASHINGTON, {**AP_821_WASHINGTON, "PIN": "9822000470"}])
    d = _decide("47.60092633", "-122.32214985", "821 S WASHINGTON ST, SEATTLE WA 98104",
                point_parcels=(("9822000470", "C"), ("9822000480", "C")))
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "multiple_pins")


def test_only_lettered_king_addresses_for_an_unlettered_lead_is_rejected(monkeypatch):
    _gis(monkeypatch, points=AP_1212_ALLEN, under_point=[PARCEL_1212A])
    d = _decide("47.66083408", "-122.34308247", "1212 N ALLEN PL, SEATTLE WA 98103")
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "lettered_only")


def test_a_letter_king_does_not_have_is_rejected(monkeypatch):
    _gis(monkeypatch, points=[AP_5452])
    d = _decide("47.55", "-122.36", "5452 B 25TH AVE SW, SEATTLE WA 98106",
                point_parcels=(("1773101445", "R"),))
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "no_address_point")


def test_a_point_with_no_pin_is_rejected(monkeypatch):
    _gis(monkeypatch, points=[AP_3919_PASADENA])
    d = _decide("47.65", "-122.32", "3919 PASADENA PL NE, SEATTLE WA 98105", point_parcels=())
    assert (d.outcome, d.evidence["reason"]) == ("rejected", "no_pin")


def test_a_condo_complex_is_never_an_address_point(monkeypatch):
    _gis(monkeypatch, points=[AP_4616_MLK], under_point=[PARCEL_4616_MLK])
    d = _decide("47.56185593", "-122.29364595", "4616 M L KING JR WAY S, SEATTLE WA 98108")
    assert (d.outcome, d.pin, d.match) == ("condo_complex", "8562990000", "condo_complex")
    ed = kap.decision_fields(d, checked_at="t")
    assert located_parcel_id(ed) is None and mailing_lookup_pin(ed) is None
    # The condo extract flag alone is enough, whatever the polygon says.
    _gis(monkeypatch, points=[AP_4616_MLK])
    d = _decide("47.56", "-122.29", "4616 MARTIN LUTHER KING JR WAY S",
                point_parcels=(("8562990000", "C"),))
    assert d.match == "condo_complex"


def test_a_transient_failure_records_nothing(monkeypatch):
    def _down(*a, **kw):
        raise ConnectionError("reset")

    monkeypatch.setattr(kap, "safe_get", _down)
    d = _decide("47.52", "-122.35", "9043 A 18TH AVE SW, SEATTLE WA 98106")
    assert d.outcome == "error" and kap.decision_fields(d, checked_at="t") == {}
    monkeypatch.setattr(kap, "safe_get", lambda *a, **kw: _Resp(None, status_code=503))
    assert _decide("47.52", "-122.35", "9043 A 18TH AVE SW").outcome == "error"


# ── Read side: shown, named, never paid for ────────────────────────────────────

def _ap_ed(**over) -> dict:
    return {"source": "seattle_sdci_code_violations", "record_number": "013845-26CP",
            "kc_pin": "7899800716", "kc_pin_status": "matched",
            "kc_pin_source": "king_gis_address_point", "kc_pin_match": "address_point",
            "latitude": "47.52163912", "longitude": "-122.35809767", **over}


def _row(ed, **over) -> dict:
    base = {"id": str(uuid.uuid4()), "date_recorded": "08/23/2026", "party_name": None, "heirs": None,
            "legal_description": "013845-26CP", "parcel_id": None,
            "property_address": "9043 A 18TH AVE SW, SEATTLE WA 98106", "mailing_address": None,
            "enrichment_data": ed, "created_at": "2026-09-13T00:00:00Z"}
    base.update(over)
    return base


def test_an_address_point_is_shown_and_named_but_not_exact():
    ed = _ap_ed()
    assert located_parcel_id(ed) == "7899800716" and located_parcel_match(ed) == "address_point"
    assert located_parcel_id(ed, exact_only=True) is None
    assert mailing_lookup_pin(ed) == "7899800716"
    row = SimpleNamespace(party_name=None, enrichment_data=ed)
    assert kpl.owner_lookup_pins([row]) == {"7899800716": [row]}


@pytest.mark.parametrize("ed", [
    _ap_ed(kc_pin_source="king_gis_point_in_parcel"),  # tier stamped by the wrong rule
    _ap_ed(kc_pin_match="exact"),  # exact claimed by the address-point rule
    _ap_ed(kc_pin_source=["king_gis_address_point"]),
    _ap_ed(kc_pin_status="address_mismatch"),
])
def test_a_tier_is_only_trusted_from_the_rule_that_can_produce_it(ed):
    assert located_parcel_id(ed) is None and mailing_lookup_pin(ed) is None


def test_skip_trace_never_pays_for_an_address_point_owner():
    ed = _ap_ed(owner_source="king_erealproperty", owner_pin="7899800716")
    row = SimpleNamespace(id="r1", job_id="j1", user_id="u1",
                          party_name="NAMUE KATA & ISABELLA MONGI",
                          property_address="9043 A 18TH AVE SW, SEATTLE WA 98106",
                          mailing_address="9043 A 18TH AVE SW, SEATTLE, WA 98106",
                          property_city="SEATTLE", property_state="WA", property_zip="98106",
                          enrichment_data=ed)
    assert build_pending_row_payload(row) is None
    # The tier is the only reason: the same row located exactly by the point rule is paid for.
    row.enrichment_data = {**ed, "kc_pin_source": "king_gis_point_in_parcel", "kc_pin_match": "exact"}
    assert build_pending_row_payload(row) is not None


def test_api_and_export_label_a_county_address_match():
    out = ResultRow(**_row(_ap_ed()))
    assert (out.located_parcel_id, out.located_parcel_match) == ("7899800716", "address_point")
    exported = build_lead_export_row(_row(_ap_ed()))
    assert exported["parcel_id"] == "7899800716"
    assert exported["parcel_source"] == "County address match"


def test_only_shown_tiers_may_carry_a_mailing_address():
    strict = {"kc_pin_status": "matched", "kc_pin": "0904000025",
              "kc_pin_source": "king_gis_point_in_parcel"}
    assert mailing_lookup_pin({**strict, "kc_pin_match": "exact"}) == "0904000025"
    assert mailing_lookup_pin({**strict, "kc_pin_match": "street_only"}) == "0904000025"
    # A condo complex parcel's mailing belongs to no unit owner; an untiered legacy match
    # may be one, so it waits for the tier repair.
    assert mailing_lookup_pin({**strict, "kc_pin_match": "condo_complex"}) is None
    assert mailing_lookup_pin(strict) is None
    assert mailing_lookup_pin({"kc_pin_status": "matched", "kc_pin": "0904000025"}) is None


def test_a_state_written_without_a_comma_is_not_part_of_the_street():
    p = kap.parse_lead_address("7975 Martin Luther King Jr WAY S WA")
    assert (p.street, p.zip5) == ("MARTIN LUTHER KING JR WAY S", None)
    p = kap.parse_lead_address("1212 N ALLEN PL SEATTLE WA 98103")
    assert (p.street, p.zip5, p.letter) == ("ALLEN PL", "98103", "N")


@pytest.mark.parametrize("body", [{}, {"features": None}, {"features": [None]},
                                  {"features": [{"attributes": None}]}, {"features": "x"},
                                  {"features": [{"attributes": {}}]},
                                  {"features": [{"attributes": {"PIN": "7899800716"}}]}])
def test_a_malformed_answer_is_transient_not_a_rejection(body, monkeypatch):
    monkeypatch.setattr(kap, "safe_get", lambda *a, **kw: _Resp(body))
    monkeypatch.setattr(kap.time, "sleep", lambda s: None)
    d = _decide("47.52", "-122.35", "9043 A 18TH AVE SW, SEATTLE WA 98106")
    assert d.outcome == "error" and kap.decision_fields(d, checked_at="t") == {}


@pytest.mark.asyncio
async def test_the_mailing_backfill_never_relocates_an_address_point_row(
    db, business_user, tmp_path, monkeypatch,
):
    spec = importlib.util.spec_from_file_location(
        "backfill_king_cv_mailing_ap", _SCRIPT.parent / "backfill_king_code_violation_mailing.py")
    mail = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mail)
    ed = {**_ap_ed(), "kc_address_point_evidence": {"outcome": "accepted"}}
    rid, _, _ = await _stored_row(db, business_user, address="9043 A 18TH AVE SW, SEATTLE WA 98106",
                                  ed=ed)
    # Even the live path's revert shape (point outcome, no evidence) keeps a status.
    reverted, _, _ = await _stored_row(db, business_user, address="9043 A 18TH AVE SW, SEATTLE WA 98106",
                                       ed=_stored_ed("013845-26CP", "address_mismatch",
                                                     "47.52163912", "-122.35809767"))
    gis = _gis(monkeypatch, points=[AP_9043A], under_point=[PARCEL_9043A])

    def _go():
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return mail.run(sdb, apply_writes=True, limit=None, report=None, pace_s=0)

    stats = await asyncio.to_thread(_go)
    assert stats["candidates"] == 0 and gis.calls == []
    got = (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"),
                            {"i": rid})).scalar()
    assert got == ed


def test_a_capped_answer_is_transient_not_a_rejection(monkeypatch):
    monkeypatch.setattr(kap, "safe_get", lambda *a, **kw: _Resp(
        {"features": [{"attributes": AP_9043A}], "exceededTransferLimit": True}))
    monkeypatch.setattr(kap.time, "sleep", lambda s: None)
    assert _decide("47.52", "-122.35", "9043 A 18TH AVE SW").outcome == "error"
    gis = _Gis(points=[AP_3810_GALER], parcels=[PARCEL_5318100580])

    def _capped_pin(url, params=None, **kw):
        resp = gis(url, params, **kw)
        if url == kap.PARCEL_LAYER:
            resp._payload["exceededTransferLimit"] = True
        return resp

    monkeypatch.setattr(kap, "safe_get", _capped_pin)
    assert _decide(None, None, "3810 E GALER ST, SEATTLE WA 98112").outcome == "error"


@pytest.mark.asyncio
async def test_a_transient_address_point_failure_keeps_the_point_outcome_for_the_repair(
    db, business_user, tmp_path, monkeypatch,
):
    """Live path: the point outcome is a fact and is kept; no evidence is written, so the
    --address-points repair (not the live job) is where the row is retried."""
    _gis(monkeypatch, under_point=[PARCEL_9043A])

    def _ap_down(url, params=None, **kw):
        raise ConnectionError("reset")

    monkeypatch.setattr(kap, "safe_get", _ap_down)
    decisions, _ = kpl.resolve_code_violation_mailing(
        [("a", "47.52163912", "-122.35809767", "9043 A 18TH AVE SW, SEATTLE WA 98106")],
        pace_s=0, address_points=True)
    assert decisions == {"a": {"kc_pin_status": "address_mismatch"}}

    rid, _, _ = await _stored_row(
        db, business_user, address="9043 A 18TH AVE SW, SEATTLE WA 98106",
        ed={"source": _SDCI, "record_number": "013845-26CP", "latitude": "47.52163912",
            "longitude": "-122.35809767", **decisions["a"]})
    monkeypatch.setattr(bko.time, "sleep", lambda s: None)
    stats = await asyncio.to_thread(_repair, False, tmp_path)
    assert stats["candidates"] == 1 and stats["transient_error_left_for_retry"] == 1
    _gis(monkeypatch, points=[AP_9043A, AP_9043B], under_point=[PARCEL_9043A])
    import src.scrapers.enrichment.king_rpacct as kr

    monkeypatch.setattr(kr, "resolve_pins", lambda pins: ({}, "2026-09-05"))
    stats = await asyncio.to_thread(_repair, True, tmp_path)
    assert stats["writes"] == {"written": 1, "skipped_by_write_guard": 0}
    ed = (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"),
                           {"i": rid})).scalar()
    assert located_parcel_match(ed) == "address_point"


# ── Live enrichment ────────────────────────────────────────────────────────────

async def _stored_row(db, user: User, *, address, ed, status="done", party=None,
                      mailing=None, config_id=None) -> tuple[str, str, str]:
    if config_id is None:
        config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King CV",
                               county="king", state="WA", record_type="code_violation",
                               fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                               deliver={"format": "csv", "emails": []})
        db.add(config)
        await db.commit()
        config_id = config.id
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config_id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    dedup = legacy_strong_signature(None, address)
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, parcel_id=None,
                  property_address=address, legal_description=ed.get("record_number"),
                  mailing_address=mailing, dedup_hash=dedup, skip_trace_status="not_attempted",
                  is_duplicate=False, enrichment_data=ed))
    await db.commit()
    return rid, dedup, job_id


def _no_async_wait(monkeypatch):
    async def _no_wait(_s):
        return None

    monkeypatch.setattr(kca.asyncio, "sleep", _no_wait)


@pytest.mark.asyncio
async def test_a_live_job_resolves_a_lettered_townhome_names_its_owner_and_fills_mailing(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    from tests.test_king_rpacct_mailing import _acct, _extract, _use_extract

    base = {"source": "seattle_sdci_code_violations", "record_type": "Complaint"}
    townhome, dedup, job_id = await _stored_row(
        db, business_user, status="enriching", address="9043 A 18TH AVE SW, SEATTLE WA 98106",
        ed={**base, "record_number": "013845-26CP", "latitude": "47.52163912",
            "longitude": "-122.35809767"})
    # Same job, no coordinates: the County address alone never names or mails anyone.
    galer = str(uuid.uuid4())
    db.add(Result(id=galer, user_id=business_user.id, job_id=job_id, party_name=None,
                  property_address="3810 E GALER ST, SEATTLE WA 98112",
                  legal_description="1063822-CT", mailing_address=None,
                  skip_trace_status="not_attempted", is_duplicate=False,
                  enrichment_data={**base, "record_number": "1063822-CT",
                                   "latitude": None, "longitude": None}))
    await db.commit()

    _gis(monkeypatch, points=[AP_9043A, AP_9043B, AP_3810_GALER], under_point=[PARCEL_9043A],
         parcels=[PARCEL_5318100580])
    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("789980", "0716", "9043 A 18TH AVE SW", "SEATTLE WA", "98106"),
        _acct("531810", "0580", "3810 E GALER ST", "SEATTLE WA", "98112")]))
    pages: list = []

    def _erp(url, *a, **kw):
        pages.append(url)
        return _Resp(None, text_body=PAGE_7899800716)

    monkeypatch.setattr(kca, "safe_get", _erp)
    _no_async_wait(monkeypatch)
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis",
                        lambda *a, **kw: {})

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, job_id)
            config = sdb.get(ScraperConfig, job.scraper_config_id)
            _run_inline_enrichment(sdb, job, redis_client, job_id, config, summary={})

    await asyncio.to_thread(_go)

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, party_name, parcel_id, dedup_hash, mailing_address, enrichment_data "
        "FROM results WHERE id = ANY(:ids)"), {"ids": [townhome, galer]})).all()}
    t, g = got[townhome], got[galer]
    assert t.enrichment_data["kc_pin_match"] == "address_point"
    assert t.enrichment_data["kc_address_point_evidence"]["point_status"] == "address_mismatch"
    assert t.party_name == "NAMUE KATA & ISABELLA MONGI"
    assert t.enrichment_data["owner_pin"] == "7899800716"
    assert t.mailing_address == "9043 A 18TH AVE SW, SEATTLE, WA 98106"
    assert (t.parcel_id, t.dedup_hash) == (None, dedup)
    assert pages == [pages[0]] and pages[0].endswith("7899800716")
    assert g.enrichment_data["kc_pin_status"] == "matched"
    assert g.enrichment_data["kc_pin_match"] == "address_only"
    assert (g.party_name, g.mailing_address) == (None, None)


@pytest.mark.asyncio
async def test_an_unusable_extract_leaves_an_address_point_match_for_the_repair(monkeypatch):
    _gis(monkeypatch, points=[AP_9043A, AP_9043B], under_point=[PARCEL_9043A])
    import src.scrapers.enrichment.king_rpacct as kr

    monkeypatch.setattr(kr, "resolve_pins", lambda pins: None)
    decisions, snapshot = kpl.resolve_code_violation_mailing(
        [("a", "47.52163912", "-122.35809767", "9043 A 18TH AVE SW, SEATTLE WA 98106")],
        pace_s=0, address_points=True)
    assert (decisions, snapshot) == ({"a": {"kc_pin_status": "address_mismatch"}}, None)


# ── Historical repair (--address-points) ───────────────────────────────────────

_SDCI = "seattle_sdci_code_violations"


def _stored_ed(record, status, lat=None, lon=None):
    return {"source": _SDCI, "record_number": record, "kc_pin_status": status,
            "latitude": lat, "longitude": lon}


def _repair(apply_writes, tmp_path):
    from src.db.session import system_sync_session

    with system_sync_session() as sdb:
        return bko.run_address_points(sdb, apply_writes=apply_writes,
                                      report=tmp_path / "ap.jsonl", gis_pace_s=0)


@pytest.mark.asyncio
async def test_address_point_repair_is_dry_by_default_guarded_and_converges(
    db, business_user, tmp_path, monkeypatch,
):
    from tests.test_king_rpacct_mailing import _acct, _extract, _use_extract

    townhome, dedup, _ = await _stored_row(
        db, business_user, address="9043 A 18TH AVE SW, SEATTLE WA 98106",
        ed=_stored_ed("013845-26CP", "address_mismatch", "47.52163912", "-122.35809767"))
    allen, _, _ = await _stored_row(
        db, business_user, address="1212 N ALLEN PL, SEATTLE WA 98103",
        ed=_stored_ed("014018-26CP", "address_mismatch", "47.66083408", "-122.34308247"))
    dravus, _, _ = await _stored_row(  # already has a mailing address: never replaced
        db, business_user, address="2001 W DRAVUS ST, SEATTLE WA 98199",
        mailing="PO BOX 1, SEATTLE, WA 98111",
        ed=_stored_ed("014047-26CP", "address_mismatch", "47.64827278", "-122.38223017"))
    galer, _, _ = await _stored_row(
        db, business_user, address="3810 E GALER ST, SEATTLE WA 98112",
        ed=_stored_ed("1063822-CT", "no_coordinates"))
    live, _, _ = await _stored_row(
        db, business_user, status="enriching", address="9043 A 18TH AVE SW, SEATTLE WA 98106",
        ed=_stored_ed("013845-26CP", "address_mismatch", "47.52163912", "-122.35809767"))

    def _under(url, params=None, **kw):  # the point's parcels depend on the point
        lat = params["geometry"].split(",")[1] if params and "geometry" in params else None
        gis.under_point = {"47.52163912": [PARCEL_9043A], "47.66083408": [PARCEL_1212A],
                           "47.64827278": [PARCEL_2003_DRAVUS]}.get(lat, [])
        return _Gis.__call__(gis, url, params, **kw)

    gis = _gis(monkeypatch, points=[AP_9043A, AP_9043B, *AP_1212_ALLEN, AP_2001_DRAVUS,
                                    AP_3810_GALER], parcels=[PARCEL_5318100580])
    monkeypatch.setattr(kap, "safe_get", _under)
    monkeypatch.setattr(bko.time, "sleep", lambda s: None)
    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("789980", "0716", "9043 A 18TH AVE SW", "SEATTLE WA", "98106"),
        _acct("277060", "2445", "1 OWNER WAY", "BELLEVUE WA", "98004")]))

    ids = [townhome, allen, dravus, galer, live]
    select = text("SELECT id, party_name, parcel_id, dedup_hash, mailing_address, enrichment_data "
                  "FROM results WHERE id = ANY(:ids)")
    before = {str(r.id): (r.mailing_address, r.enrichment_data)
              for r in (await db.execute(select, {"ids": ids})).all()}

    dry = await asyncio.to_thread(_repair, False, tmp_path)
    assert dry["candidates"] == 4 and "writes" not in dry
    db.expire_all()
    assert {str(r.id): (r.mailing_address, r.enrichment_data)
            for r in (await db.execute(select, {"ids": ids})).all()} == before

    stats = await asyncio.to_thread(_repair, True, tmp_path)
    assert stats["writes"] == {"written": 4, "skipped_by_write_guard": 0}
    assert stats["tier_address_point"] == 2 and stats["tier_address_only"] == 1
    assert stats["reason_lettered_only"] == 1 and stats["mailing_found"] == 1

    db.expire_all()
    got = {str(r.id): r for r in (await db.execute(select, {"ids": ids})).all()}
    t = got[townhome]
    assert t.enrichment_data["kc_pin"] == "7899800716"
    assert located_parcel_match(t.enrichment_data) == "address_point"
    assert t.mailing_address == "9043 A 18TH AVE SW, SEATTLE, WA 98106"
    assert t.enrichment_data["mailing_source"] == "king_rpacct"
    assert (t.parcel_id, t.dedup_hash, t.party_name) == (None, dedup, None)
    assert got[allen].enrichment_data["kc_pin_status"] == "address_mismatch"
    assert got[allen].enrichment_data["kc_address_point_evidence"]["reason"] == "lettered_only"
    assert "kc_pin" not in got[allen].enrichment_data and got[allen].mailing_address is None
    assert got[dravus].mailing_address == "PO BOX 1, SEATTLE, WA 98111"
    assert "mailing_source" not in got[dravus].enrichment_data
    assert located_parcel_id(got[dravus].enrichment_data) == "2770602445"
    assert got[galer].enrichment_data["kc_pin_match"] == "address_only"
    assert got[galer].mailing_address is None
    assert got[live].enrichment_data == before[live][1]

    again = await asyncio.to_thread(_repair, True, tmp_path)
    assert again["candidates"] == 0


@pytest.mark.asyncio
async def test_address_point_repair_write_skips_a_row_changed_since_it_was_read(db, business_user):
    stored = _stored_ed("013845-26CP", "address_mismatch", "47.52163912", "-122.35809767")
    rid, _, _ = await _stored_row(
        db, business_user, address="9043 A 18TH AVE SW, SEATTLE WA 98106", ed=stored)
    payload = ('{"kc_pin_status": "matched", "kc_pin": "7899800716", '
               '"kc_pin_match": "address_point", "kc_pin_source": "king_gis_address_point", '
               '"kc_address_point_evidence": {"outcome": "accepted"}}')

    def _write(**over):
        from src.db.session import system_sync_session

        params = {"new_mail": None, "old_mail": None, "rid": rid, "uid": business_user.id,
                  "payload": payload, "source": _SDCI, "f_property_state": None,
                  "f_owner_state": None, "f_absentee": None, "f_out_of_state": None,
                  "old_pin_status": "address_mismatch", "old_pin": None, "old_pin_match": None,
                  "old_pin_source": None, "old_parcel_address": None,
                  "old_address": "9043 A 18TH AVE SW, SEATTLE WA 98106", "old_zip": None,
                  "old_lat": "47.52163912", "old_lon": "-122.35809767", "old_city": None,
                  "old_state": None, "old_ed": json.dumps(stored), **over}
        with system_sync_session() as sdb:
            res = sdb.execute(text(bko._AP_UPDATE_SQL), params)
            sdb.commit()
            return res.rowcount

    assert await asyncio.to_thread(_write, old_pin_status="multiple") == 0
    assert await asyncio.to_thread(_write, old_mail="PO BOX 1, SEATTLE, WA 98111") == 0
    assert await asyncio.to_thread(_write, uid=str(uuid.uuid4())) == 0
    assert await asyncio.to_thread(_write, old_parcel_address="9043A 18TH AVE SW") == 0
    # The decision was made for this address and point; an edited input is not ours.
    assert await asyncio.to_thread(_write, old_address="9043 B 18TH AVE SW, SEATTLE WA 98106") == 0
    assert await asyncio.to_thread(_write, old_zip="98106") == 0
    assert await asyncio.to_thread(_write, old_lat="47.52158159") == 0
    assert await asyncio.to_thread(_write, old_state="WA") == 0
    # Any other change to the object read (e.g. a kc_pin_checked_at stamped since) too.
    assert await asyncio.to_thread(
        _write, old_ed=json.dumps({**stored, "kc_pin_checked_at": "2026-09-13T00:00:00+00:00"})) == 0
    assert await asyncio.to_thread(_write) == 1
    # Decided once: a second decision for the same row is refused.
    assert await asyncio.to_thread(_write, old_pin_status="matched", old_pin="7899800716",
                                   old_pin_match="address_point",
                                   old_pin_source="king_gis_address_point") == 0


def test_address_points_run_alone(capsys):
    with pytest.raises(SystemExit):
        bko.main(["--address-points", "--owners"])
