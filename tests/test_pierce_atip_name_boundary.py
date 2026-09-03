"""The RCW 42.56.070(8) boundary must be ENFORCED, not merely documented.

Product decision 2026-09-03: the Pierce ATIP fallback stays, and the "never the
taxpayer name" boundary gets a hard guard so a later edit cannot quietly widen it.
Before this, the boundary held only because nothing happened to read row["name"].
"""

import pytest

from src.scrapers.enrichment.pierce_atip import (
    _ALLOWED_OUT_KEYS,
    _assert_address_only,
    _is_person_key,
    parse_summary,
)

# Shape taken from the live ATIP summary API (2026-09-02 verification run).
_ROW = {
    "situs": "10608 63RD STREET CT E #49",
    "mail": "10608 63RD STREET CT E",
    "city": "PUYALLUP",
    "state": "WA",
    "zip": "98372-5803",
    "acct_type": "Mobile Home",
    "use_cd": "Mobile Home",
    "name": "BOICOURT JACQUELINE L",
}


def test_the_taxpayer_name_is_never_returned():
    out = parse_summary(dict(_ROW))
    assert set(out) <= _ALLOWED_OUT_KEYS
    blob = " ".join(str(v) for v in out.values() if v).upper()
    assert "BOICOURT" not in blob
    assert "JACQUELINE" not in blob


def test_the_real_address_still_comes_through():
    out = parse_summary(dict(_ROW))
    assert out["property_address"] == "10608 63RD STREET CT E #49"
    assert out["mailing_address"] == "10608 63RD STREET CT E, PUYALLUP, WA, 98372-5803"
    assert out["atip_account_type"] == "Mobile Home"


def test_a_name_in_the_mail_block_is_stripped_not_stored():
    # No production row looks like this today, but an assessor mail block may
    # conventionally lead with the addressee — that must not reach the app.
    row = dict(_ROW, mail="BOICOURT JACQUELINE L", mail2="10608 63RD STREET CT E")
    out = parse_summary(row)
    assert "BOICOURT" not in (out["mailing_address"] or "").upper()
    assert "10608 63RD STREET CT E" in out["mailing_address"]


def test_guard_rejects_any_non_address_field():
    with pytest.raises(AssertionError, match="address fields"):
        _assert_address_only({"property_address": "x", "owner_name": "SMITH JOHN"})


def test_no_situs_is_still_none():
    assert parse_summary({"situs": "", "name": "SMITH JOHN"}) is None


def test_addressee_prefixes_do_not_smuggle_a_name_through():
    from src.scrapers.enrichment.pierce_atip import parse_summary
    for lead in ("C/O BOICOURT JACQUELINE L", "ATTN BOICOURT JACQUELINE L",
                 "ATTN: BOICOURT JACQUELINE L", "c/o Boicourt Jacqueline L"):
        row = {"situs": "1 A ST", "name": "BOICOURT JACQUELINE L",
               "mail": lead, "mail2": "10608 63RD STREET CT E",
               "city": "PUYALLUP", "state": "WA", "zip": "98373"}
        out = parse_summary(row)
        assert "BOICOURT" not in (out["mailing_address"] or "").upper(), lead
        assert "10608 63RD STREET CT E" in out["mailing_address"]


def test_a_name_sharing_a_line_with_the_street_is_excised():
    """The assessor conventionally puts addressee and street on ONE line."""
    from src.scrapers.enrichment.pierce_atip import parse_summary
    row = {"situs": "1 A ST", "taxpayer_name": "BOICOURT JACQUELINE L",
           "mail": "BOICOURT JACQUELINE L 10608 63RD STREET CT E",
           "city": "PUYALLUP", "state": "WA", "zip": "98373"}
    out = parse_summary(row)
    mailing = out["mailing_address"] or ""
    assert "BOICOURT" not in mailing.upper()
    assert "10608 63RD STREET CT E" in mailing


def test_an_unseen_person_field_name_still_gets_excluded():
    """A fixed key tuple would have missed these; the boundary must fail closed."""
    from src.scrapers.enrichment.pierce_atip import parse_summary
    for key in ("taxpayerName", "owner1", "mail_name", "TaxPayer_NM", "addressee"):
        row = {"situs": "1 A ST", key: "BOICOURT JACQUELINE L",
               "mail": "BOICOURT JACQUELINE L", "mail2": "10608 63RD STREET CT E",
               "city": "PUYALLUP", "state": "WA", "zip": "98373"}
        out = parse_summary(row)
        assert "BOICOURT" not in (out["mailing_address"] or "").upper(), key


def test_an_address_field_that_merely_spells_name_is_not_treated_as_a_person():
    """Over-broad key matching would excise a REAL street from the mailing line."""
    from src.scrapers.enrichment.pierce_atip import parse_summary
    row = {"situs": "1 A ST", "street_name": "63RD STREET CT E",
           "mail": "10608 63RD STREET CT E",
           "city": "PUYALLUP", "state": "WA", "zip": "98373"}
    out = parse_summary(row)
    assert "10608 63RD STREET CT E" in (out["mailing_address"] or "")


def test_the_real_prod_shape_is_unchanged():
    """All 31 ATIP rows in prod carry a street and no name — must pass through."""
    from src.scrapers.enrichment.pierce_atip import parse_summary
    row = {"situs": "9605 187TH ST E", "mail": "9605 187TH ST E",
           "city": "PUYALLUP", "state": "WA", "zip": "98375"}
    out = parse_summary(row)
    assert out["mailing_address"] == "9605 187TH ST E, PUYALLUP, WA, 98375"


# ── Two defects found reviewing the Codex-authored hardening (2026-09-03) ──────
# Both are the SAME over-broad-guard mistake the code's own comments warn about:
# a guard that removes more than it can prove is a person.


def test_an_owner_prefixed_ADDRESS_key_is_not_a_person():
    # `owner_address` is address data that merely mentions the owner. Treating it
    # as a person harvested the STREET into the exclusion list and then deleted
    # that street from mailing_address (measured: mail became "PUYALLUP, WA, 98372").
    for key in ("owner_address", "owner_city", "taxpayer_addr", "owner_mail_line1",
                "street_name", "situs_name"):
        assert _is_person_key(key) is False, key
    for key in ("name", "owner", "taxpayer", "addressee", "attn", "mail_name",
                "owner_name", "taxpayerName"):
        assert _is_person_key(key) is True, key


def test_an_owner_address_key_does_not_eat_the_street():
    row = dict(_ROW, owner_address="10608 63RD STREET CT E")
    out = parse_summary(row)
    assert out["mailing_address"] == "10608 63RD STREET CT E, PUYALLUP, WA, 98372-5803"


def test_a_short_name_is_not_excised_from_inside_a_street():
    # A bare substring test cut "LEE" out of "LEELAND ST" and produced "123 LAND ST"
    # — a FABRICATED address, worse than the leak the guard exists to prevent.
    row = {"situs": "123 LEELAND ST", "mail": "123 LEELAND ST", "city": "TACOMA",
           "state": "WA", "zip": "98402", "name": "LEE"}
    out = parse_summary(row)
    assert out["mailing_address"] == "123 LEELAND ST, TACOMA, WA, 98402"


def test_the_name_is_still_stripped_when_it_shares_the_street_line():
    # The guard must still do its job after both fixes.
    row = {"situs": "x", "mail": "BOICOURT JACQUELINE L", "mail2": "10608 63RD ST E",
           "city": "PUYALLUP", "state": "WA", "zip": "98372",
           "name": "BOICOURT JACQUELINE L"}
    out = parse_summary(row)
    assert "BOICOURT" not in (out["mailing_address"] or "").upper()
    assert "10608 63RD ST E" in out["mailing_address"]
