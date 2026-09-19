"""The v2 lookup subject key: what may reuse a paid answer, and what may not.

The legacy key hashed the address and no owner name, so inside the 90-day window
one address had one answer and an heir's lead could be served the deceased
owner's phone. These tests pin the D1 matrix (owner decision 2026-09-19) field by
field, plus the normalization and the read/write agreement that make the key
usable without re-paying for traces we already bought.
"""

import pytest

from src.scrapers.enrichment.skip_trace import (
    _SUBJECT_FIELD_MAX,
    lookup_subject_key,
    payload_subject_key,
    pending_row_subject_key,
    submission_collision_key,
)

USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"
ADDR = "123 Main St"
CITY = "Tacoma"
STATE = "WA"


def _normal(user=USER_A, addr=ADDR, city=CITY, state=STATE, first="Alice", last="Smith"):
    return lookup_subject_key(user, addr, city, state, "normal", first, last)


def _advanced(user=USER_A, addr=ADDR, city=CITY, state=STATE, first=None, last=None):
    return lookup_subject_key(user, addr, city, state, "advanced", first, last)


# ─── D1 matrix: normal is owner-isolated ──────────────────────────────────────

def test_normal_different_owner_same_address_does_not_reuse():
    """The bug this whole phase exists for: the deceased owner, then the heir."""
    assert _normal(first="Alice", last="Smith") != _normal(first="Bob", last="Jones")


def test_normal_same_owner_same_address_reuses():
    assert _normal() == _normal()


def test_normal_same_owner_differing_only_by_case_and_padding_reuses():
    assert _normal(first="ALICE", last="  smith ") == _normal(first="alice", last="Smith")


# ─── D1 matrix: advanced is address-scoped, by design ─────────────────────────

def test_advanced_same_address_reuses_regardless_of_owner():
    """No name was ever sent, so owner isolation cannot apply (D1, documented)."""
    assert _advanced() == _advanced()


def test_advanced_ignores_names_it_is_handed():
    """Round 13 condition: an advanced subject hashes null names whatever the
    payload or party_name says. Hashing a name that was never sent would split
    one bought answer into two keys and re-pay for it."""
    assert _advanced(first="Alice", last="Smith") == _advanced(first=None, last=None)
    assert _advanced(first="Bob", last="Jones") == _advanced(first="Alice", last="Smith")


def test_advanced_still_isolated_by_account():
    assert _advanced(user=USER_A) != _advanced(user=USER_B)


def test_advanced_still_keyed_on_address():
    assert _advanced() != _advanced(addr="456 Oak Ave")


# ─── The two trace types never read each other's answers ──────────────────────

def test_normal_never_reuses_advanced_and_the_reverse():
    assert _normal() != _advanced()


def test_normal_with_missing_name_does_not_collide_with_advanced():
    """Both hash null names; only trace_type separates them. Without it in the
    key, a 1-credit normal answer and a 2-credit advanced answer would merge."""
    assert lookup_subject_key(USER_A, ADDR, CITY, STATE, "normal", None, None) != _advanced()


# ─── Tenant isolation ─────────────────────────────────────────────────────────

def test_tenant_isolation_holds_for_normal():
    assert _normal(user=USER_A) != _normal(user=USER_B)


# ─── Normalization: collapse what is formatting, keep what is meaning ─────────

def test_address_case_and_internal_whitespace_collapse():
    assert _normal(addr="123  MAIN   st") == _normal(addr="123 Main St")


def test_unicode_whitespace_collapses():
    assert _normal(addr="123 Main St") == _normal(addr="123 Main St")


def test_nfkc_folds_compatibility_forms():
    assert _normal(addr="１２３ Main St") == _normal(addr="123 Main St")


def test_state_case_is_not_meaning():
    assert _normal(state="wa") == _normal(state="WA")


def test_punctuation_is_preserved():
    """The legacy key stripped '.', ',' and '#'. Keeping them invents no
    equivalence: a unit number is part of the address, not formatting."""
    assert _normal(addr="123 Main St #2") != _normal(addr="123 Main St 2")
    assert _normal(addr="123 Main St.") != _normal(addr="123 Main St")


def test_missing_name_is_distinct_from_empty_name():
    """JSON null and '' are different values, so a row that never had a name and
    a row whose name was blanked do not silently share an answer."""
    assert _normal(first=None, last=None) != _normal(first="", last="")


def test_a_field_cannot_impersonate_a_field_boundary():
    """The legacy key joined on '|', so a value containing '|' could shift the
    fields. The JSON array cannot be forged that way."""
    assert _normal(addr="a|b", city="c") != _normal(addr="a", city="b|c")


def test_unknown_trace_type_is_rejected():
    with pytest.raises(ValueError):
        lookup_subject_key(USER_A, ADDR, CITY, STATE, "express", None, None)


# ─── Read/write agreement: truncation must not split one answer in two ────────

class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_long_name_hits_its_own_cache_entry():
    """The enqueue hashes the payload BEFORE the pending row exists; the ingest
    hashes the row AFTER the insert truncated it to the column width. If the key
    did not truncate the same way, every long name would miss its own cache entry
    and the trace would be re-paid (the bug enrich.py:2354 records for the
    address column, reachable through the names v2 adds)."""
    long_last = "Van Der " + ("Berg" * 60)  # comfortably past the 128 column
    assert len(long_last) > _SUBJECT_FIELD_MAX

    payload = {
        "property_address": ADDR, "city": CITY, "state": STATE,
        "trace_type": "normal", "first_name": "Alice", "last_name": long_last,
    }
    stored = _Row(
        user_id=USER_A, property_address=ADDR, city=CITY, state=STATE,
        trace_type="normal", first_name="Alice",
        last_name=long_last[:_SUBJECT_FIELD_MAX],  # what the insert actually stores
    )
    assert payload_subject_key(USER_A, payload) == pending_row_subject_key(stored)


def test_payload_and_row_agree_on_an_ordinary_subject():
    payload = {
        "property_address": ADDR, "city": CITY, "state": STATE,
        "trace_type": "normal", "first_name": "Alice", "last_name": "Smith",
    }
    stored = _Row(
        user_id=USER_A, property_address=ADDR, city=CITY, state=STATE,
        trace_type="normal", first_name="Alice", last_name="Smith",
    )
    assert payload_subject_key(USER_A, payload) == pending_row_subject_key(stored)


# ─── The submission key is a different job (round 14, 14-A) ───────────────────

def test_two_owners_at_one_address_share_a_submission_key():
    """Provider attribution is address-only and refuses a group when two answers
    come back for one address. So these two must not go out together, even though
    the cache now treats them as two subjects."""
    a = submission_collision_key(ADDR, CITY, STATE, "normal")
    b = submission_collision_key(ADDR, CITY, STATE, "normal")
    assert a == b
    assert _normal(first="Alice", last="Smith") != _normal(first="Bob", last="Jones")


def test_submission_key_is_global_across_tenants():
    """Attribution carries no tenant identifier, so a cross-tenant pair at one
    address is refused the same way and must be serialized the same way. The key
    takes no user_id at all, which is what makes that true by construction."""
    import inspect
    assert "user_id" not in inspect.signature(submission_collision_key).parameters


def test_submission_key_separates_trace_types():
    assert (
        submission_collision_key(ADDR, CITY, STATE, "normal")
        != submission_collision_key(ADDR, CITY, STATE, "advanced")
    )


def test_submission_key_never_equals_a_subject_key():
    """Separate namespaces: one can never be read as the other."""
    assert submission_collision_key(ADDR, CITY, STATE, "normal") != _normal()
    assert submission_collision_key(ADDR, CITY, STATE, "advanced") != _advanced()
