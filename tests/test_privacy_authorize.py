"""The privacy authorization service (src/privacy/authorize.py; Phase 6 P3d).

The database refuses any rule state but 'pending' until P7 (migration 125), so the
decision rules are proven on rule histories built in memory with ``build_snapshot`` (the
loader's own core), and the loader itself is proven against the real table with the rows
the database allows today. Keys come from the test keyring (pytest monkeypatch), never a
production secret. Synthetic names and numbers only.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.session import sync_engine
from src.privacy import keys
from src.privacy.authorize import (
    ALLOWED,
    REVIEW,
    REVIEW_SUBJECT_WITHOUT_ANCHOR,
    REVIEW_UNRESOLVED_SUBJECT,
    ROW_COLUMNS,
    WITHHELD,
    RuleSnapshotError,
    authorize,
    build_snapshot,
    load_rule_snapshot,
)
from src.privacy.subjects import NORMALIZATION_VERSION
from src.workers.property_identity import compute_property_key, property_key_input

_T0 = datetime(2026, 11, 6, tzinfo=UTC)
_PARCEL = keys.property_anchor_value  # alias for readability below
_CTX = {"record_type": "probate", "county": "king", "state": "WA"}


@pytest.fixture(autouse=True)
def _keyring(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "PII_ENCRYPTION_STRICT", False)
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", f"v1:{'a' * 40},v2:{'b' * 40}")
    monkeypatch.setattr(settings, "PRIVACY_HMAC_CURRENT_VERSION", "v1")
    keys.reset_keyring_cache()
    yield
    keys.reset_keyring_cache()


# ── the property anchor ──────────────────────────────────────────────────────

@pytest.mark.parametrize(("parcel", "addr", "county", "state"), [
    ("123456-7890", "1 Main St", "King", "wa"),
    (None, "1400 Main Street, Seattle", "king", "WA"),
    ("12", "x", "king", "WA"),
    ("R-1234 567", None, "Pierce", "WA"),
])
def test_the_property_key_is_byte_identical_after_the_refactor(parcel, addr, county, state):
    """compute_property_key keys results.property_key and property_list_membership:
    sha256 over exactly the shared input, as before."""
    key_input = property_key_input(parcel, addr, county, state)
    expected = hashlib.sha256(key_input.encode()).hexdigest() if key_input else None
    assert compute_property_key(parcel, addr, county, state) == expected
    # Pinned once, so a change to the input format itself cannot pass silently.
    assert compute_property_key("123456-7890", "1 Main St", "King", "wa") == hashlib.sha256(
        b"P|WA|king|1234567890").hexdigest()


def test_the_property_anchor_is_the_scoped_parcel_only():
    assert _PARCEL("123456-7890", "King", "wa") == "P|WA|king|1234567890"
    assert _PARCEL(None, "king", "WA") is None          # no parcel: never an address anchor
    assert _PARCEL("12", "king", "WA") is None          # too weak to identify a property
    assert _PARCEL("1234567890", "", "WA") is None      # unscoped: repeats across counties
    assert _PARCEL("1234567890", "king", None) is None
    assert len(keys.privacy_hmac("property", "P|WA|king|1234567890")) == 64


@pytest.mark.parametrize("value", [
    "P|wa|king|1234567890",        # state not upper-cased
    "P|WA|King|1234567890",        # county not lower-cased
    "P|WA|king|1234-567890",       # parcel not normalised
    "P|WA||1234567890",            # no county
    "A|WA|king|1400 MAIN ST",      # the address branch is never keyed
    "P|WA|king|1234|ATTACKER",     # anything after the parcel
    "1400 MAIN ST",
])
def test_a_non_canonical_or_address_property_value_is_refused(value):
    with pytest.raises(ValueError):
        keys.privacy_hmac("property", value)


# ── rule histories ───────────────────────────────────────────────────────────

def _h(kind: str, value: str, version: str = "v1") -> str:
    return keys.privacy_hmac(kind, value, version)


def _history(states, *, anchor=("property", "P|WA|king|1234567890"),
             subject=("person", "smith|john"), version="v1", norm=NORMALIZATION_VERSION,
             scope="subject", rule_id=None, step=timedelta(minutes=1)):
    rule_id = rule_id or str(uuid.uuid4())
    return [{
        "id": str(uuid.uuid4()), "rule_id": rule_id, "anchor_kind": anchor[0],
        "anchor_hmac": _h(anchor[0], anchor[1], version) if anchor[0] != "mailing" else "0" * 64,
        "subject_kind": subject[0], "subject_hmac": _h(subject[0], subject[1], version),
        "scope": scope, "key_version": version, "normalization_version": norm,
        "state": state, "created_at": _T0 + i * step,
    } for i, state in enumerate(states)]


def test_only_a_pending_then_active_history_is_enforced():
    assert build_snapshot(_history(["pending"])).active_rules == 0
    assert build_snapshot(_history(["pending", "active"])).active_rules == 1
    assert build_snapshot(_history(["pending", "active", "lifted"])).active_rules == 0


def test_a_tie_in_one_transaction_is_ordered_by_the_lifecycle():
    rows = _history(["active", "pending"], step=timedelta(0))  # same created_at
    assert build_snapshot(rows).active_rules == 1


@pytest.mark.parametrize("states", [
    ["active"], ["lifted"], ["pending", "lifted"], ["pending", "pending"],
    ["pending", "active", "active"], ["active", "pending"], ["pending", "unknown"],
])
def test_any_other_history_fails_the_load(states):
    with pytest.raises(RuleSnapshotError):
        build_snapshot(_history(states))


@pytest.mark.parametrize("over", [
    {"norm": "n2"},
    {"scope": "property_all_persons"},
    {"anchor": ("mailing", "anything")},
])
def test_an_active_rule_that_cannot_be_evaluated_fails_the_load(over):
    with pytest.raises(RuleSnapshotError):
        build_snapshot(_history(["pending", "active"], **over))


def test_an_active_rule_under_a_key_that_is_no_longer_live_fails_the_load(monkeypatch):
    rows = _history(["pending", "active"], version="v2")
    monkeypatch.setattr(settings, "PRIVACY_HMAC_KEYS", f"v1:{'a' * 40}")
    keys.reset_keyring_cache()
    with pytest.raises(RuleSnapshotError):
        build_snapshot(rows)


def test_an_inactive_rule_under_an_old_version_is_ignored(monkeypatch):
    rows = _history(["pending"], norm="n1")
    assert build_snapshot(rows).active_rules == 0


# ── decisions ────────────────────────────────────────────────────────────────

def _snap(*histories):
    return build_snapshot([r for h in histories for r in h])


def _row(**over):
    return {"party_name": "SMITH JOHN", "heirs": None, "parcel_id": "123456-7890",
            "phone": None, "phones": None, "email": None, "emails": None,
            "enrichment_data": None, **over}


@pytest.mark.parametrize("column", ROW_COLUMNS)
def test_a_projection_missing_any_column_fails_closed_while_a_rule_is_active(column):
    partial = {k: v for k, v in _row().items() if k != column}
    with pytest.raises(RuleSnapshotError):
        authorize(_snap(_history(["pending", "active"])), partial, **_CTX)
    # No active rule: nothing is read, so nothing can be missed.
    assert authorize(_snap(_history(["pending"])), partial, **_CTX).outcome == ALLOWED


def test_no_active_rule_allows_everything_without_keying_anything(monkeypatch):
    snapshot = _snap(_history(["pending"]))
    monkeypatch.setattr(keys, "privacy_hmac", None)  # proves the fast path keys nothing
    assert authorize(snapshot, _row(), **_CTX).outcome == ALLOWED


def test_the_rules_person_on_the_rules_property_is_withheld():
    rows = _history(["pending", "active"])
    d = authorize(_snap(rows), _row(), **_CTX)
    assert d.outcome == WITHHELD and d.suppression_ids == (rows[1]["id"],)
    assert not d.deliverable


def test_a_co_owner_row_is_withheld_whole():
    d = authorize(_snap(_history(["pending", "active"])),
                  _row(party_name="DOE JANE & SMITH JOHN"), **_CTX)
    assert d.outcome == WITHHELD


def test_someone_else_on_the_same_property_is_allowed():
    d = authorize(_snap(_history(["pending", "active"])), _row(party_name="JONES MARY"), **_CTX)
    assert d.outcome == ALLOWED and d.deliverable


def test_an_unreadable_name_on_the_rules_property_goes_to_review():
    d = authorize(_snap(_history(["pending", "active"])), _row(),
                  record_type=None, county="king", state="WA")  # no declared name order
    assert (d.outcome, d.reason) == (REVIEW, REVIEW_UNRESOLVED_SUBJECT)
    assert not d.deliverable


@pytest.mark.parametrize("over", [
    {"parcel_id": None},                 # address-only row: no anchor at all
    {"parcel_id": "999999-0000"},        # the person, on another parcel
])
def test_the_rules_person_without_its_anchor_goes_to_review(over):
    d = authorize(_snap(_history(["pending", "active"])), _row(**over), **_CTX)
    assert (d.outcome, d.reason) == (REVIEW, REVIEW_SUBJECT_WITHOUT_ANCHOR)


# ── operator releases of REVIEW matches (migration 136) ──────────────────────

def _released(rows, result_id, rule_id=None):
    return build_snapshot(rows, [{"rule_id": rule_id or rows[0]["rule_id"],
                                  "result_id": result_id}])


@pytest.mark.parametrize("over, ctx", [
    ({"parcel_id": None}, _CTX),                                         # without anchor
    ({}, {"record_type": None, "county": "king", "state": "WA"}),       # unresolved name
])
def test_a_released_review_match_is_allowed_for_that_result_only(over, ctx):
    rows = _history(["pending", "active"])
    rid = str(uuid.uuid4())
    snapshot = _released(rows, rid)
    assert authorize(snapshot, _row(id=rid, **over), **ctx).outcome == ALLOWED
    other = authorize(snapshot, _row(id=str(uuid.uuid4()), **over), **ctx)
    assert other.outcome == REVIEW


def test_a_release_never_frees_an_exact_match():
    rows = _history(["pending", "active"])
    rid = str(uuid.uuid4())
    assert authorize(_released(rows, rid), _row(id=rid), **_CTX).outcome == WITHHELD


def test_a_release_for_one_rule_leaves_another_rules_review_in_place():
    first = _history(["pending", "active"])
    second = _history(["pending", "active"])  # the same person under a second request
    rid = str(uuid.uuid4())
    snapshot = build_snapshot(first + second,
                              [{"rule_id": first[0]["rule_id"], "result_id": rid}])
    d = authorize(snapshot, _row(id=rid, parcel_id=None), **_CTX)
    assert d.outcome == REVIEW and d.suppression_ids == (second[1]["id"],)


def test_a_row_without_an_id_is_never_released():
    rows = _history(["pending", "active"])
    snapshot = _released(rows, "None")  # even a release keyed by the string "None"
    assert authorize(snapshot, _row(parcel_id=None), **_CTX).outcome == REVIEW


def test_an_heir_is_a_subject_too():
    d = authorize(_snap(_history(["pending", "active"])),
                  _row(party_name="JONES MARY", heirs="SMITH JOHN"), **_CTX)
    assert d.outcome == WITHHELD


def test_a_phone_in_the_extra_contacts_is_an_anchor():
    rows = _history(["pending", "active"], anchor=("phone", "2065551234"))
    d = authorize(_snap(rows), _row(parcel_id=None, phones=[
        {"number": "(425) 555-0000", "type": "Mobile"},
        {"number": "(206) 555-1234", "type": "Landline"}]), **_CTX)
    assert d.outcome == WITHHELD


def test_an_email_anchor_matches_in_any_case():
    rows = _history(["pending", "active"], anchor=("email", "jsmith@example.test"))
    d = authorize(_snap(rows), _row(parcel_id=None, emails=["JSmith@Example.test"]), **_CTX)
    assert d.outcome == WITHHELD


def test_a_rule_keyed_under_the_new_version_matches_mid_rotation():
    rows = _history(["pending", "active"], version="v2")
    assert authorize(_snap(rows), _row(), **_CTX).outcome == WITHHELD


# ── the loader against the real table ────────────────────────────────────────

def test_the_loader_reads_every_rule_row_and_enforces_none_today():
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            rid = conn.execute(text(
                "INSERT INTO privacy_requests (kind, channel, deadline_at) "
                "VALUES ('opt_out', 'web', now() + interval '45 days') RETURNING id")).scalar()
            conn.execute(text(
                "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, "
                "anchor_hmac, subject_kind, subject_hmac, scope, key_version, "
                "normalization_version, state) VALUES (gen_random_uuid(), :r, 'property', "
                ":a, 'person', :s, 'subject', 'v1', :n, 'pending')"),
                {"r": rid, "a": _h("property", "P|WA|king|1234567890"),
                 "s": _h("person", "smith|john"), "n": NORMALIZATION_VERSION})
            snapshot = load_rule_snapshot(conn)
            assert snapshot.active_rules == 0
            assert authorize(snapshot, _row(), **_CTX).outcome == ALLOWED
        finally:
            trans.rollback()


def test_a_load_that_fails_is_a_snapshot_error_not_an_empty_snapshot():
    conn = sync_engine.connect()
    conn.close()
    with pytest.raises(RuleSnapshotError):
        load_rule_snapshot(conn)


def test_an_entity_rule_matches_the_entity_on_the_row():
    rows = _history(["pending", "active"], subject=("entity", "smith family trust"))
    d = authorize(_snap(rows), _row(party_name="SMITH FAMILY TRUST"), **_CTX)
    assert d.outcome == WITHHELD


# ── owner of record (enrichment_data, exported as "current_owner") ───────────

def _owned(owner, **over):
    return _row(party_name="JONES MARY", enrichment_data={"assessor_current_owner": owner,
                                                          "title_status": "differs"}, **over)


def test_the_rules_person_as_owner_of_record_is_withheld():
    rows = _history(["pending", "active"])
    d = authorize(_snap(rows), _owned("SMITH JOHN"), **_CTX)
    assert d.outcome == WITHHELD and d.suppression_ids == (rows[1]["id"],)


def test_an_owner_of_record_co_owner_withholds_the_whole_row():
    d = authorize(_snap(_history(["pending", "active"])), _owned("DOE JANE & SMITH JOHN"), **_CTX)
    assert d.outcome == WITHHELD


def test_an_entity_owner_of_record_matches_an_entity_rule():
    rows = _history(["pending", "active"], subject=("entity", "smith family trust"))
    assert authorize(_snap(rows), _owned("SMITH FAMILY TRUST"), **_CTX).outcome == WITHHELD


def test_an_unreadable_owner_of_record_on_the_rules_property_goes_to_review():
    d = authorize(_snap(_history(["pending", "active"])), _owned("ИВАНОВ ИВАН"), **_CTX)
    assert (d.outcome, d.reason) == (REVIEW, REVIEW_UNRESOLVED_SUBJECT)


def test_the_rules_person_as_owner_without_its_anchor_goes_to_review():
    d = authorize(_snap(_history(["pending", "active"])), _owned("SMITH JOHN", parcel_id=None),
                  **_CTX)
    assert (d.outcome, d.reason) == (REVIEW, REVIEW_SUBJECT_WITHOUT_ANCHOR)


@pytest.mark.parametrize("enrichment", [None, {}, {"assessor_current_owner": None},
                                        {"title_status": "differs"}, "not a mapping"])
def test_no_owner_of_record_leaves_the_decision_to_the_other_subjects(enrichment):
    snapshot = _snap(_history(["pending", "active"]))
    other = _row(party_name="JONES MARY", enrichment_data=enrichment)
    assert authorize(snapshot, other, **_CTX).outcome == ALLOWED
    named = _row(enrichment_data=enrichment)  # party_name SMITH JOHN
    assert authorize(snapshot, named, **_CTX).outcome == WITHHELD


def test_a_scraped_record_is_read_by_attribute_including_its_owner():
    from src.scrapers.base_scraper import ScrapedRecord
    rec = ScrapedRecord(party_name="JONES MARY", parcel_id="123456-7890",
                        enrichment_data={"assessor_current_owner": "SMITH JOHN"})
    assert authorize(_snap(_history(["pending", "active"])), rec, **_CTX).outcome == WITHHELD


def test_the_primary_phone_and_email_are_anchors():
    phone = _history(["pending", "active"], anchor=("phone", "2065551234"))
    email = _history(["pending", "active"], anchor=("email", "jsmith@example.test"))
    assert authorize(_snap(phone), _row(parcel_id=None, phone="206-555-1234"),
                     **_CTX).outcome == WITHHELD
    assert authorize(_snap(email), _row(parcel_id=None, email="jsmith@example.test"),
                     **_CTX).outcome == WITHHELD


def test_an_unreadable_contact_is_no_anchor_but_the_person_still_goes_to_review():
    """The contact decoder reads an unreadable value as None (src/utils/contact_decode.py)."""
    rows = _history(["pending", "active"], anchor=("phone", "2065551234"))
    d = authorize(_snap(rows), _row(parcel_id=None, phone=None, phones=[None, {"number": None}],
                                    email=None, emails=[None]), **_CTX)
    assert (d.outcome, d.reason) == (REVIEW, REVIEW_SUBJECT_WITHOUT_ANCHOR)


def test_a_keyring_that_breaks_fails_the_load_and_the_decision(monkeypatch):
    rows = _history(["pending", "active"])
    snapshot = _snap(rows)
    monkeypatch.setattr(settings, "PRIVACY_HMAC_CURRENT_VERSION", "not-a-version")
    keys.reset_keyring_cache()
    with pytest.raises(RuleSnapshotError):
        build_snapshot(rows)
    with pytest.raises(RuleSnapshotError):
        authorize(snapshot, _row(), **_CTX)
