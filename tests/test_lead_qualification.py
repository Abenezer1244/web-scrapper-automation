"""Lead qualification policy (src/api/lead_qualification.py): pure, no DB.

Owner decision 2026-10-25: a code-enforcement case is a deliverable lead only with a
verified owner (named by its own county's assessor, for the parcel the lead shows), that
parcel, a property address, an owner mailing address and the case itself. Phone and
email are never required. The SQL twin is checked against real Postgres in
tests/test_lead_qualification_worker.py, never against a copy of this module.
"""
from types import SimpleNamespace

import pytest

from src.api.lead_actionability import is_actionable
from src.api.lead_qualification import (
    ADDRESS_AMBIGUOUS,
    ENRICHMENT_PENDING,
    INVALID_CASE_DATA,
    MAILING_UNRESOLVED,
    MISSING_OWNER,
    MISSING_PROPERTY_ADDRESS,
    NEEDS_REVIEW,
    NOT_QUALIFIED_STATE,
    OWNER_CONFLICT,
    PARCEL_UNRESOLVED,
    POLICY_CODE_VIOLATION,
    POLICY_KEY,
    QUALIFIED,
    UNSUPPORTED_SOURCE,
    is_qualified,
    is_subject,
    qualification_reasons,
    qualification_state,
    qualified_sql,
    stamp,
)

PIN = "1234567890"


def seattle(**kw):
    """A Seattle SDCI case located exactly on a parcel and named by King's assessor."""
    ed = {
        POLICY_KEY: POLICY_CODE_VIOLATION,
        "source": "seattle_sdci_code_violations",
        "record_number": "1047891-CF",
        "record_type": "Complaint",
        "violation_category": "Weeds",
        "status": "Under Investigation",
        "kc_pin_status": "matched", "kc_pin": PIN, "kc_pin_match": "exact",
        "kc_pin_source": "king_gis_point_in_parcel",
        "owner_source": "king_erealproperty", "owner_pin": PIN,
    }
    ed.update(kw.pop("ed", {}))
    row = {"party_name": "HARTLEY MARGARET A", "parcel_id": None,
           "property_address": "4512 S HOLLY ST",
           "mailing_address": "4512 S HOLLY ST, SEATTLE, WA 98118",
           "date_recorded": "10/02/2026", "phone": None, "email": None,
           "enrichment_data": ed}
    row.update(kw)
    return row


def bellevue(parcel="0123456789", **kw):
    """A Bellevue case: the source prints the PIN, which keeps its leading zero."""
    ed = {
        POLICY_KEY: POLICY_CODE_VIOLATION, "source": "bellevue_code_enforcement",
        "case_number": "25-112233-EA", "case_type": "Property Maintenance",
        "status": "Open", "owner_source": "king_erealproperty", "owner_pin": parcel,
    }
    ed.update(kw.pop("ed", {}))
    row = {"party_name": "LAKESIDE HOLDINGS LLC", "parcel_id": parcel,
           "property_address": "1420 140TH AVE NE",
           "mailing_address": "PO BOX 4401, BELLEVUE, WA 98009",
           "date_recorded": "09/30/2026", "enrichment_data": ed}
    row.update(kw)
    return row


# ── 1, 6, 29: a complete case qualifies without any contact data ─────────────

def test_complete_case_qualifies_with_no_phone_or_email():
    row = seattle()
    assert qualification_reasons(row) == []
    assert is_qualified(row) and qualification_state(row) == QUALIFIED
    assert is_actionable(row)
    assert is_qualified(SimpleNamespace(**row))  # attribute access, like ORM rows


def test_printed_parcel_case_qualifies():
    assert qualification_reasons(bellevue()) == []


# ── 2-5: each mandatory field on its own ─────────────────────────────────────

@pytest.mark.parametrize("field, reason", [
    ("party_name", MISSING_OWNER),
    ("property_address", MISSING_PROPERTY_ADDRESS),
    ("mailing_address", MAILING_UNRESOLVED),
])
@pytest.mark.parametrize("blank", [None, "", "   ", "(enrichment unavailable)"])
def test_each_mandatory_field_is_required(field, reason, blank):
    row = seattle(**{field: blank})
    assert reason in qualification_reasons(row)
    assert not is_qualified(row) and not is_actionable(row)


def test_missing_parcel_is_held():
    row = seattle(ed={"kc_pin_status": "no_parcel", "kc_pin": None, "kc_pin_match": None,
                      "kc_pin_source": None, "owner_source": None, "owner_pin": None},
                  party_name=None)
    reasons = qualification_reasons(row)
    assert PARCEL_UNRESOLVED in reasons and MISSING_OWNER in reasons
    assert qualification_state(row) == NOT_QUALIFIED_STATE


# ── 7-9: a name that is not the assessor's owner of this parcel ──────────────

@pytest.mark.parametrize("name, owner_source", [
    ("Weeds — 4512 S HOLLY ST", None),           # the case label / description
    ("JANE COMPLAINANT", None),                   # a complainant typed into the name
    ("TENANT OF RECORD", "lease_roll"),           # a tenant from another feed
])
def test_a_name_without_assessor_provenance_is_rejected(name, owner_source):
    row = seattle(party_name=name, ed={"owner_source": owner_source})
    assert OWNER_CONFLICT in qualification_reasons(row)
    assert qualification_state(row) == NEEDS_REVIEW
    assert not is_actionable(row)


# ── 10: trusts, LLCs and multiple legal owners are ordinary owners ───────────

@pytest.mark.parametrize("name", ["LAKESIDE HOLDINGS LLC", "HARTLEY FAMILY TRUST",
                                  "NGUYEN TUAN+LINH", "H5 CAPITAL - SEATTLE REAL E"])
def test_entity_and_joint_owners_qualify(name):
    assert is_qualified(bellevue(party_name=name))


# ── 11, 12: ambiguous locations are never auto-qualified ─────────────────────

@pytest.mark.parametrize("pin_fields", [
    {"kc_pin_status": "multiple", "kc_pin": None, "kc_pin_match": None},
    {"kc_pin_status": "address_mismatch", "kc_pin": None, "kc_pin_match": None},
    {"kc_pin_match": "condo_complex"},             # a whole condo complex parcel
    {"kc_pin_match": "address_only", "kc_pin_source": "king_gis_address_point"},
])
def test_ambiguous_or_multi_parcel_location_needs_review(pin_fields):
    row = seattle(ed=pin_fields)
    assert ADDRESS_AMBIGUOUS in qualification_reasons(row)
    assert qualification_state(row) == NEEDS_REVIEW


def test_a_tier_stamped_by_the_wrong_locator_is_not_a_parcel():
    # An address-point tier claimed by the strict point locator proves nothing.
    row = seattle(ed={"kc_pin_match": "address_point"})
    assert PARCEL_UNRESOLVED in qualification_reasons(row)


# ── 13, 14: county and parcel identity ───────────────────────────────────────

def test_an_owner_from_another_countys_assessor_is_rejected():
    tacoma = bellevue(parcel="2012345678", ed={"source": "tacoma_code_violations",
                                              "owner_source": "king_erealproperty"})
    assert OWNER_CONFLICT in qualification_reasons(tacoma)
    king = bellevue(ed={"owner_source": "pierce_atip"})
    assert OWNER_CONFLICT in qualification_reasons(king)
    assert is_qualified(bellevue(parcel="2012345678", ed={
        "source": "tacoma_code_violations", "owner_source": "pierce_atip"}))


def test_leading_zero_parcel_is_preserved_and_compared_exactly():
    assert is_qualified(bellevue(parcel="0123456789"))
    stripped_owner = bellevue(parcel="0123456789", ed={"owner_pin": "123456789"})
    assert OWNER_CONFLICT in qualification_reasons(stripped_owner)


@pytest.mark.parametrize("parcel", ["(enrichment unavailable)", "N/A", "0123-456789", "１２３４５６７８９０"])
def test_text_that_is_not_a_ten_digit_pin_is_not_a_parcel(parcel):
    row = bellevue(parcel=parcel)  # owner_pin carries the same text, so only shape decides
    assert PARCEL_UNRESOLVED in qualification_reasons(row)


@pytest.mark.parametrize("key", ["case_number", "case_type"])
def test_placeholder_case_text_is_not_case_data(key):
    assert INVALID_CASE_DATA in qualification_reasons(
        bellevue(ed={key: "(enrichment unavailable)"}))


def test_json_number_case_reference_counts_like_postgres_reads_it():
    # ->> turns a JSON number into its text, so the SQL twin accepts it; so must Python.
    assert is_qualified(bellevue(ed={"case_number": 25112233}))


@pytest.mark.parametrize("name", [" ", "  ", "--", "\t\n"])
def test_a_name_with_no_letter_or_digit_is_not_an_owner(name):
    assert MISSING_OWNER in qualification_reasons(bellevue(party_name=name))


def test_a_number_is_never_an_owner_pin():
    assert OWNER_CONFLICT in qualification_reasons(
        bellevue(parcel="1000000000", ed={"owner_pin": 1e9}))


@pytest.mark.parametrize("ed", [{"source": ["bellevue_code_enforcement"]},
                                {"source": {"a": 1}}, {"kc_pin_status": ["multiple"]}])
def test_malformed_json_values_never_raise(ed):
    assert isinstance(qualification_reasons(bellevue(ed=ed)), list)
    qualification_state(bellevue(ed=ed))


@pytest.mark.parametrize("ed", [
    {"case_number": True}, {"case_number": ["25-1"]}, {"case_number": {"n": "25-1"}},
    {"case_type": 7}, {"case_type": ["Property Maintenance"]},
])
def test_case_metadata_must_be_scalar(ed):
    assert INVALID_CASE_DATA in qualification_reasons(bellevue(ed=ed))


def test_unknown_source_is_unsupported():
    row = bellevue(ed={"source": "some_new_city"})
    assert qualification_reasons(row) == [UNSUPPORTED_SOURCE]


# ── 15-17: retryable vs settled vs conflicting ───────────────────────────────

def test_owner_not_yet_resolved_is_retryable_not_success():
    # Source outage or timeout: the parcel is known, the owner lookup did not answer.
    row = seattle(party_name=None, ed={"owner_source": None, "owner_pin": None})
    assert qualification_state(row) == ENRICHMENT_PENDING
    assert not is_actionable(row)


def test_owner_settled_absent_is_not_qualified():
    row = seattle(party_name=None, ed={"owner_source": None, "owner_pin": None,
                                       "cv_owner_recovery_outcome": "not_on_record"})
    assert qualification_state(row) == NOT_QUALIFIED_STATE


def test_owner_named_for_a_different_parcel_needs_review():
    row = seattle(ed={"owner_pin": "9999999999"})
    assert OWNER_CONFLICT in qualification_reasons(row)
    assert qualification_state(row) == NEEDS_REVIEW


@pytest.mark.parametrize("ed, kw", [
    ({"record_number": None}, {}),
    ({"record_type": None, "violation_category": None}, {}),
    ({}, {"date_recorded": None}),
])
def test_case_reference_classification_and_date_are_required(ed, kw):
    assert INVALID_CASE_DATA in qualification_reasons(seattle(ed=ed, **kw))


# ── scope: only stamped rows ─────────────────────────────────────────────────

def test_unstamped_rows_keep_the_previous_rule():
    legacy = seattle(party_name=None, mailing_address=None)
    legacy["enrichment_data"] = {k: v for k, v in legacy["enrichment_data"].items()
                                 if k != POLICY_KEY}
    assert not is_subject(legacy["enrichment_data"])
    assert is_qualified(legacy) and is_actionable(legacy)
    assert qualification_state(legacy) is None
    assert is_actionable({"property_address": "1 A ST", "mailing_address": None,
                          "enrichment_data": None})


def test_stamp_returns_a_new_dict_and_keeps_existing_keys():
    original = {"source": "x"}
    stamped = stamp(original)
    assert stamped == {"source": "x", POLICY_KEY: POLICY_CODE_VIOLATION}
    assert original == {"source": "x"}
    assert stamp(None) == {POLICY_KEY: POLICY_CODE_VIOLATION}


# ── 30: the explanation lists every failure, not just the first ──────────────

def test_reasons_name_every_failure():
    row = seattle(party_name=None, mailing_address=None, date_recorded=None,
                  ed={"owner_source": None, "owner_pin": None})
    assert qualification_reasons(row) == [MISSING_OWNER, MAILING_UNRESOLVED, INVALID_CASE_DATA]


def test_sql_twin_only_takes_an_alias():
    assert "r.enrichment_data" in qualified_sql("r")
    with pytest.raises(ValueError):
        qualified_sql("r; DROP TABLE results")


def test_owner_source_mirrors_match_the_enrichment_modules():
    from src.api.lead_qualification import KING_OWNER_SOURCE, PIERCE_OWNER_SOURCE, TACOMA
    from src.scrapers.enrichment import king_parcel_locate, pierce_atip_owner
    assert KING_OWNER_SOURCE == king_parcel_locate.OWNER_SOURCE
    assert PIERCE_OWNER_SOURCE == pierce_atip_owner.OWNER_SOURCE
    assert TACOMA == pierce_atip_owner.TACOMA_CV_SOURCE


def test_spliced_sql_survives_str_format():
    # segments.py and others splice actionable_sql into a template and then call
    # .format(); a regex quantifier like {10} would be read as a placeholder there.
    from src.api.lead_actionability import actionable_sql, deliverable_before_cap_sql, withheld_sql
    for build in (qualified_sql, actionable_sql, withheld_sql, deliverable_before_cap_sql):
        sql = build("r")
        assert "{" not in sql and "}" not in sql, build.__name__
        assert sql.format() == sql


def test_orm_twins_compose_and_negate():
    # withheld_condition negates qualified_condition; a bare text() cannot be negated.
    from sqlalchemy import and_, not_

    from src.api.lead_actionability import actionable_condition, withheld_condition
    from src.api.lead_qualification import qualified_condition
    for clause in (not_(qualified_condition()), withheld_condition(),
                   and_(actionable_condition(), qualified_condition())):
        assert "qualification_policy" in str(clause.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("field", ["party_name", "property_address", "mailing_address", "date_recorded"])
def test_a_column_value_that_is_not_text_is_never_usable(field):
    assert not is_qualified(bellevue(**{field: ["4512 S HOLLY ST"]}))
