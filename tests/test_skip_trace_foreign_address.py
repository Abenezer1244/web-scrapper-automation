"""A paid skip trace is never sent a state we made up.

The old parser read the first two letters of the last comma chunk as the state, so a
city or a country became one: '..., 123 MAIN ST #500, SEATTLE WA 98101' -> 'SE',
'..., VANCOUVER BC V6C 0A6, CANADA' -> city 'CANADA'. Tracerfy is keyed on
(address, city, state) and charges per row, so every one of those was money spent on a
place that does not exist. A prod read over 163,261 results found 153 such rows.

Address strings here are synthetic, in the shapes the prod read actually found.
"""
from types import SimpleNamespace

import pytest

from src.scrapers.enrichment.skip_trace import (
    _parse_full_address,
    build_pending_row_payload,
    legacy_cache_locality,
)
from src.utils.lead_formatting import US_STATES, is_foreign_address, strip_us_country_tail


class TestIsForeignAddress:
    @pytest.mark.parametrize("addr", [
        "2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA",
        "232 MILLVIEW PL SW CALGARY AB, CANADA",       # country glued to the city chunk
        "12381 66TH AVE, SURREY BC V3W2A3",            # Canadian postal code, no country
        "271 JOO CHIAT PL, SINGAPORE",
        "10 DOWNING ST, LONDON, UNITED KINGDOM",
        "1 REFORMA, MEXICO CITY, MEXICO",
        "1 hastings st, vancouver bc, canada",         # case-insensitive
    ])
    def test_foreign_tails_are_detected(self, addr):
        assert is_foreign_address(addr) is True

    @pytest.mark.parametrize("addr", [
        "123 MAIN ST, SEATTLE, WA 98101",
        "123 MAIN ST, SEATTLE, WA 98101, USA",
        "CANADA DR, SEATTLE, WA 98101",                # country word inside street text
        "123 UNIT A1B 2C3, SEATTLE, WA 98101",         # US unit that looks like a postal code
        "CANADA",                                      # single chunk: no tail to read
        "",
        None,
    ])
    def test_us_and_ambiguous_addresses_are_not_foreign(self, addr):
        assert is_foreign_address(addr) is False


class TestStripUsCountryTail:
    @pytest.mark.parametrize("addr,expected", [
        ("123 MAIN ST, SEATTLE, WA 98101, USA", "123 MAIN ST, SEATTLE, WA 98101"),
        ("123 MAIN ST, SEATTLE, WA 98101, US", "123 MAIN ST, SEATTLE, WA 98101"),
        ("123 MAIN ST, SEATTLE, WA 98101, UNITED STATES", "123 MAIN ST, SEATTLE, WA 98101"),
        ("123 MAIN ST, SEATTLE, WA 98101", "123 MAIN ST, SEATTLE, WA 98101"),
        ("123 US HIGHWAY 2, SEATTLE, WA 98101", "123 US HIGHWAY 2, SEATTLE, WA 98101"),
    ])
    def test_only_a_whole_country_chunk_is_removed(self, addr, expected):
        assert strip_us_country_tail(addr) == expected


class TestParseFullAddressStates:
    @pytest.mark.parametrize("addr", [
        "C/O ACME LLC, 123 MAIN ST #500, SEATTLE WA 98101",   # 'SE' off SEATTLE
        "456 OAK AVE, LAKE FOREST, PK 98155",                 # 'PK' is no state
        "789 PINE ST, FEDERAL WAY, WS 98003",                 # source typo for WA
        "1 A ST, OSAGE CITY, KA 66523",                       # source typo for KS
    ])
    def test_a_fabricated_state_is_never_emitted(self, addr):
        assert _parse_full_address(addr)["state"] is None

    @pytest.mark.parametrize("addr", [
        "10 DOWNING ST, LONDON UNITED KINGDOM",   # country glued to the city chunk
        "10 DOWNING ST LONDON UNITED KINGDOM",    # no comma at all
        "1 REFORMA MEXICO CITY, MEXICO",
        "1201-838 W HASTINGS ST VANCOUVER BC V6C 0A6",  # comma-less, postal code only
    ])
    def test_a_country_tail_the_csv_rule_misses_is_still_refused_a_trace(self, addr):
        # _looks_foreign_for_trace is wider than the shared CSV rule on the paid path.
        parsed = _parse_full_address(addr)
        assert (parsed["city"], parsed["state"], parsed["zip"]) == (None, None, None)

    @pytest.mark.parametrize("addr", [
        "1 A ST, OSAGE CITY, KA 66523",   # 3-part
        "1 A ST, OSAGE CITY KA 66523",    # 2-part (Codex P2)
    ])
    def test_a_rejected_state_still_yields_the_zip(self, addr):
        # The ZIP is real even when the state token is not — dropping both would
        # strand a row that a later locality backfill could still rescue.
        parsed = _parse_full_address(addr)
        assert parsed["zip"] == "66523"
        assert parsed["city"] == "OSAGE CITY"
        assert parsed["state"] is None

    @pytest.mark.parametrize("addr,expected", [
        ("123 MAIN ST, SEATTLE, WA 98101", ("SEATTLE", "WA", "98101")),
        ("123 MAIN ST, SEATTLE, WA98101", ("SEATTLE", "WA", "98101")),
        ("123 MAIN ST, SEATTLE, WA 98101-1234", ("SEATTLE", "WA", "98101-1234")),
        ("123 MAIN ST, SEATTLE, WA", ("SEATTLE", "WA", None)),
        ("123 MAIN ST, SEATTLE WA 98101", ("SEATTLE", "WA", "98101")),          # 2-part
        ("123 main st, seattle wa 98126", ("seattle", "WA", "98126")),          # lowercase
        ("123 MAIN ST, TACOMA, WA, 98422-1824", ("TACOMA", "WA", "98422-1824")),  # 4-part
        ("123 MAIN ST, SEATTLE, WA 98101, USA", ("SEATTLE", "WA", "98101")),    # US tail
        ("123 MAIN ST, APO, AE 09021", ("APO", "AE", "09021")),                 # military
        ("123 MAIN ST, WASHINGTON, DC 20500", ("WASHINGTON", "DC", "20500")),
        ("123 MAIN ST, SAN JUAN, PR 00901", ("SAN JUAN", "PR", "00901")),
    ])
    def test_real_us_addresses_still_parse(self, addr, expected):
        parsed = _parse_full_address(addr)
        assert (parsed["city"], parsed["state"], parsed["zip"]) == expected

    def test_every_emitted_state_is_a_real_us_code(self):
        for addr in ("C/O ACME LLC, 123 MAIN ST #500, SEATTLE WA 98101",
                     "456 OAK AVE, LAKE FOREST, PK 98155",
                     "123 MAIN ST, SEATTLE, WA 98101",
                     "2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA"):
            state = _parse_full_address(addr)["state"]
            assert state is None or state in US_STATES

    def test_a_foreign_address_is_kept_whole_and_never_split(self):
        parsed = _parse_full_address("2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA")
        assert parsed == {
            "street": "2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA",
            "city": None, "state": None, "zip": None,
        }

    def test_street_only_and_empty_are_unchanged(self):
        assert _parse_full_address("123 MAIN ST")["street"] == "123 MAIN ST"
        assert _parse_full_address("") == {"street": None, "city": None, "state": None, "zip": None}


def _parse_full_address_as_of_origin_main(addr: str) -> dict:
    """The parser exactly as it stood before this fix (origin/main 30c5e43).

    Frozen here so `legacy=True` is proved equal to the real old behaviour rather
    than to a description of it: the cache keys of already-PAID traces depend on it.
    """
    import re

    result = {"street": None, "city": None, "state": None, "zip": None}
    if not addr:
        return result
    clean = addr.strip().rstrip(",")
    parts = [p.strip() for p in clean.split(",")]
    if len(parts) >= 1:
        result["street"] = parts[0] or None
    if len(parts) == 2:
        second = parts[1].strip()
        m = re.match(r"^(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", second)
        if m:
            result["city"] = m.group(1).strip() or None
            result["state"] = m.group(2)
            result["zip"] = m.group(3)
        else:
            result["city"] = second or None
    elif len(parts) == 3:
        result["city"] = parts[1] or None
        last = parts[2]
        m = re.match(r"([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?", last.upper())
        if m:
            result["state"] = m.group(1)
            result["zip"] = m.group(2) or None
        else:
            m2 = re.match(r"(\d{5}(?:-\d{4})?)", last)
            if m2:
                result["zip"] = m2.group(1)
    elif len(parts) >= 4:
        result["city"] = parts[1] or None
        state_part = parts[2].strip().upper()
        zip_part = parts[3].strip()
        m_state = re.match(r"^([A-Z]{2})$", state_part)
        if m_state:
            result["state"] = m_state.group(1)
        m_zip = re.match(r"(\d{5}(?:-\d{4})?)", zip_part)
        if m_zip:
            result["zip"] = m_zip.group(1)
    return result


_LEGACY_CORPUS = [
    "",
    "123 MAIN ST",
    "123 MAIN ST,",
    "  123 MAIN ST , SEATTLE  ",
    "123 MAIN ST, SEATTLE",
    "123 MAIN ST, SEATTLE WA 98101",
    "123 main st, seattle wa 98101",          # lowercase: legacy 2-part regex misses
    "123 MAIN ST, SEATTLE WA 98101-1234",
    "123 MAIN ST, SEATTLE, WA 98101",
    "123 MAIN ST, SEATTLE, WA98101",
    "123 MAIN ST, SEATTLE, WA",
    "123 MAIN ST, SEATTLE, 98101",            # 3-part, no state token
    "C/O ACME LLC, 123 MAIN ST #500, SEATTLE WA 98101",
    "456 OAK AVE, LAKE FOREST, PK 98155",
    "789 PINE ST, FEDERAL WAY, WS 98003",
    "1 A ST, OSAGE CITY, KA 66523",
    "1 A ST, OSAGE CITY KA 66523",
    "123 MAIN ST, TACOMA, WA, 98422-1824",
    "123 MAIN ST, TACOMA, WASH, 98422",       # 4-part, non-2-letter state
    "123 MAIN ST, TACOMA, WA, NOT-A-ZIP",
    "123 MAIN ST, SEATTLE, WA 98101, USA",
    "2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA",
    "1201-838 W HASTINGS ST VANCOUVER BC V6C 0A6, CANADA",
    "232 MILLVIEW PL SW CALGARY AB, CANADA",
    "10 DOWNING ST, LONDON, UNITED KINGDOM",
    "271 JOO CHIAT PL, SINGAPORE 427952",
]


class TestLegacyModeIsByteForByte:
    """`legacy=True` must keep reproducing the OLD (wrong) parse — an already-PAID
    trace is cached under that key, and a corrected key would buy the row twice."""

    @pytest.mark.parametrize("addr", _LEGACY_CORPUS)
    def test_legacy_equals_the_frozen_old_parser_field_for_field(self, addr):
        assert _parse_full_address(addr, legacy=True) == _parse_full_address_as_of_origin_main(addr)

    def test_the_corpus_actually_exercises_the_difference(self):
        # Guards the test above from proving nothing if the two modes ever converge.
        differing = [a for a in _LEGACY_CORPUS
                     if _parse_full_address(a) != _parse_full_address(a, legacy=True)]
        assert len(differing) >= 8

    @pytest.mark.parametrize("addr,old_state", [
        ("C/O ACME LLC, 123 MAIN ST #500, SEATTLE WA 98101", "SE"),
        ("456 OAK AVE, LAKE FOREST, PK 98155", "PK"),
        ("789 PINE ST, FEDERAL WAY, WS 98003", "WS"),
    ])
    def test_legacy_still_returns_the_invented_state(self, addr, old_state):
        assert _parse_full_address(addr, legacy=True)["state"] == old_state

    def test_legacy_still_splits_a_foreign_address(self):
        parsed = _parse_full_address("1201-838 W HASTINGS ST VANCOUVER BC V6C 0A6, CANADA",
                                     legacy=True)
        assert parsed["city"] == "CANADA"

    def test_legacy_cache_key_for_a_foreign_mail_row_is_unchanged(self):
        r = SimpleNamespace(
            property_address="806 W ARGAND ST",
            mailing_address="2402 W 33RD AVE VANCOUVER BC V6M 1C3, CANADA",
        )
        assert legacy_cache_locality(r) == ("CANADA", None)


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


class TestPayloadWithForeignAddresses:
    def test_a_foreign_mailing_address_sends_no_mail_fields(self):
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, SEATTLE, WA 98101",
            mailing_address="2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA",
        ))
        assert payload is not None
        assert (payload["city"], payload["state"]) == ("SEATTLE", "WA")
        assert payload["mail_address"] is None
        assert payload["mail_city"] is None
        assert payload["mail_state"] is None
        assert payload["mail_zip"] is None

    def test_a_foreign_mailing_address_never_supplies_the_locality(self):
        # Street-only property + foreign mail = no US locality anywhere -> declined,
        # instead of a paid trace of 'CANADA'.
        assert build_pending_row_payload(_result(
            property_address="806 W ARGAND ST",
            mailing_address="2402 W 33RD AVE, VANCOUVER BC V6M 1C3, CANADA",
        )) is None

    def test_a_foreign_property_address_is_declined_even_with_a_stored_situs(self):
        assert build_pending_row_payload(_result(
            property_address="12381 66TH AVE, SURREY BC V3W2A3",
            mailing_address=None,
            property_city="SEATTLE", property_state="WA", property_zip="98101",
        )) is None

    @pytest.mark.parametrize("prop", [
        "10 DOWNING ST LONDON UNITED KINGDOM",     # comma-less
        "10 DOWNING ST, LONDON UNITED KINGDOM",    # country glued to the city chunk
        "1201-838 W HASTINGS ST VANCOUVER BC V6C 0A6",  # comma-less, postal code only
    ])
    def test_a_glued_country_tail_never_borrows_a_us_situs(self, prop):
        # The payload guard must use the WIDENED rule, or a stored SEATTLE/WA situs
        # would buy a trace of a London property (Codex round 2, P1).
        assert build_pending_row_payload(_result(
            property_address=prop, mailing_address=None,
            property_city="SEATTLE", property_state="WA", property_zip="98101",
        )) is None

    def test_a_glued_country_tail_in_the_mailing_line_sends_no_mail_fields(self):
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, SEATTLE, WA 98101",
            mailing_address="10 DOWNING ST, LONDON UNITED KINGDOM",
        ))
        assert payload is not None
        assert (payload["mail_address"], payload["mail_city"],
                payload["mail_state"], payload["mail_zip"]) == (None, None, None, None)

    def test_a_mailing_address_with_an_invented_state_is_not_traced(self):
        assert build_pending_row_payload(_result(
            property_address="11011 GREENWOOD AVE N",
            mailing_address="456 OAK AVE, LAKE FOREST, PK 98155",
        )) is None

    def test_a_us_mailing_address_is_unaffected(self):
        payload = build_pending_row_payload(_result())
        assert (payload["city"], payload["state"], payload["zip"]) == ("TACOMA", "WA", "98422-1824")
        assert payload["mail_state"] == "WA"

    def test_a_us_country_tail_still_traces(self):
        payload = build_pending_row_payload(_result(
            property_address="123 MAIN ST, SEATTLE, WA 98101, USA", mailing_address=None,
        ))
        assert payload is not None
        assert (payload["city"], payload["state"], payload["zip"]) == ("SEATTLE", "WA", "98101")
