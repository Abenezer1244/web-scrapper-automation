"""Skip-trace eligibility gate + pending-row payload (src/scrapers/enrichment/skip_trace.py).

Pure unit tests, no DB. Pins the 2026-09-02 "Test 1" root causes:

1. `looks_like_non_personal_party_name` substring-matched " ave"/" way"/… so real
   people named AVELINO / AVERY / WAYNE / WAYLAND were classified "not a person"
   and silently dropped from skip trace (43 rows / 12 jobs in 90 days of prod).
2. `build_pending_row_payload` hard-coded every mail_* field to None, so the
   mailing address the app already held never reached Tracerfy.
"""
from types import SimpleNamespace

import pytest

from src.scrapers.enrichment.skip_trace import (
    build_pending_row_payload,
    looks_like_non_personal_party_name,
)


class TestNonPersonalGate:
    @pytest.mark.parametrize("name", [
        # Real people whose name tokens START with a street-suffix spelling —
        # the exact prod false positives.
        "SAARENAS AVELINO G",
        "STRONG WAYNE C",
        "FOSBERG WAYNE G",
        "COLEMAN WAYLAND SR",
        "BILLINGSLEY WAYNE/CAROL",
        "KAWAHARA DICK+KIMURA WAYNE+",
        "AVERY JOHN",
        "BERNATH DAVID WAYNE EST OF",
        "TERRY MARY",
        "SMITH JOHN DR",           # DR as a name token, not a street suffix
        "JOHNSON WILLIAM EST OF",
    ])
    def test_person_names_pass(self, name):
        assert looks_like_non_personal_party_name(name) is False

    @pytest.mark.parametrize("name", [
        # Entities named after their street still own a real property; the
        # advanced (address-only) trace ignores the name, so they must NOT be
        # suppressed here — classify_grantor_as_entity routes them.
        "1423 1ST AVE LLC",
        "4807 15TH AVE S LLC",
        "COLBY AVE JOINT VENTURE LLC",
        "CALIFORNIA AVENUE HOMES LLC",
    ])
    def test_street_named_entities_pass(self, name):
        assert looks_like_non_personal_party_name(name) is False

    @pytest.mark.parametrize("name", [
        # Code-violation case descriptions — no party to trace.
        "LandLord/Tenant ? 419 21ST AVE",
        "Weeds ? 1819 HARVARD AVE",
        "Construction - 100 MAIN ST",
        "Land Use ? 12 PINE ST",
        "Noise complaint",
        "Derelict vehicle ? 5 ELM RD",
        # The party name IS a bare street address.
        "419 21ST AVE",
        "1819 HARVARD AVE",
        "12 PINE ST APT 3",
    ])
    def test_case_descriptions_and_bare_addresses_fail(self, name):
        assert looks_like_non_personal_party_name(name) is True

    def test_empty_is_not_flagged(self):
        assert looks_like_non_personal_party_name(None) is False
        assert looks_like_non_personal_party_name("") is False


def _result(**overrides):
    base = {
        "job_id": "job-1",
        "id": "res-1",
        "user_id": "user-1",
        "party_name": "SAARENAS AVELINO G",
        "property_address": "5128 BEVERLY AVE NE",
        "mailing_address": "5128 BEVERLY AVE NE, TACOMA, WA, 98422-1824",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPendingRowPayload:
    def test_avelino_is_eligible_again(self):
        payload = build_pending_row_payload(_result())
        assert payload is not None
        # Pierce GIS: street-only property + 4-part mailing → locality from mailing
        assert payload["city"] == "TACOMA"
        assert payload["state"] == "WA"
        assert payload["zip"] == "98422-1824"

    def test_mail_fields_are_populated_from_mailing_address(self):
        payload = build_pending_row_payload(_result(
            mailing_address="7105 27TH ST W, UNIVERSITY PLACE, WA, 98466-4623",
        ))
        assert payload["mail_address"] == "7105 27TH ST W"
        assert payload["mail_city"] == "UNIVERSITY PLACE"
        assert payload["mail_state"] == "WA"
        assert payload["mail_zip"] == "98466-4623"

    def test_no_mailing_address_leaves_mail_fields_none(self):
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, TACOMA, WA 98401", mailing_address=None,
        ))
        assert payload is not None
        assert payload["city"] == "TACOMA"
        assert payload["mail_address"] is None
        assert payload["mail_city"] is None
        assert payload["mail_state"] is None
        assert payload["mail_zip"] is None

    def test_statewide_shaped_property_address_carries_locality(self):
        # Any source that stores a full "STREET, CITY, WA ZIP" property_address
        # (recorder notices, King assessor) must still yield locality with no mailing.
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, OLYMPIA, WA 98501", mailing_address=None,
        ))
        assert payload["property_address"] == "123 MAIN ST"
        assert (payload["city"], payload["state"], payload["zip"]) == ("OLYMPIA", "WA", "98501")

    def test_no_property_address_is_ineligible(self):
        assert build_pending_row_payload(_result(property_address=None)) is None

    def test_case_description_is_ineligible(self):
        assert build_pending_row_payload(_result(party_name="Weeds ? 1819 HARVARD AVE")) is None


class TestStructuredSitusLocality:
    """#188 stopped fabricating a mailing line out of the situs but nothing read
    the structured columns it stores instead, so statewide-enriched rows reached
    Tracerfy with city/state/zip all None and errored out (2026-09-03)."""

    def test_situs_parts_supply_locality_when_address_is_street_only(self):
        payload = build_pending_row_payload(_result(
            property_address="9226 175TH STREET CT E",
            mailing_address=None,
            property_city="PUYALLUP", property_state="WA", property_zip="98375",
        ))
        assert payload is not None
        assert payload["property_address"] == "9226 175TH STREET CT E"
        assert (payload["city"], payload["state"], payload["zip"]) == (
            "PUYALLUP", "WA", "98375")

    def test_situs_parts_fill_independently(self):
        # A parsed city with no state/zip must still gain the stored parts.
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, OLYMPIA",
            mailing_address=None,
            property_city=None, property_state="WA", property_zip="98501",
        ))
        assert payload["city"] == "OLYMPIA"
        assert (payload["state"], payload["zip"]) == ("WA", "98501")

    def test_parsed_address_wins_over_stored_parts(self):
        # property_address is the authoritative line when it carries locality.
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, OLYMPIA, WA 98501",
            mailing_address=None,
            property_city="WRONGTOWN", property_state="OR", property_zip="99999",
        ))
        assert (payload["city"], payload["state"], payload["zip"]) == (
            "OLYMPIA", "WA", "98501")

    def test_blank_stored_parts_are_ignored(self):
        payload = build_pending_row_payload(_result(
            property_address="9226 175TH STREET CT E", mailing_address=None,
            property_city="   ", property_state="", property_zip=None,
        ))
        assert payload["city"] is None and payload["state"] is None

    def test_mailing_fallback_is_atomic_never_blended_with_situs(self):
        """An absentee owner's mailing ZIP must never be pinned onto the
        property's own city — that invents a locality that exists nowhere."""
        payload = build_pending_row_payload(_result(
            property_address="9226 175TH STREET CT E",
            mailing_address="1 OTHER ST, SEATTLE, WA 98101",
            property_city="PUYALLUP", property_state=None, property_zip=None,
        ))
        # City came from the situs, so the Seattle mailing line contributes
        # NOTHING to the property locality.
        assert payload["city"] == "PUYALLUP"
        assert payload["zip"] is None
        # ...but it is still sent as the owner's mailing address.
        assert payload["mail_city"] == "SEATTLE"
        assert payload["mail_zip"] == "98101"

    def test_mailing_still_used_when_there_is_no_situs_at_all(self):
        payload = build_pending_row_payload(_result(
            property_address="9226 175TH STREET CT E",
            mailing_address="9226 175TH STREET CT E, PUYALLUP, WA 98375",
            property_city=None, property_state=None, property_zip=None,
        ))
        assert (payload["city"], payload["state"], payload["zip"]) == (
            "PUYALLUP", "WA", "98375")
