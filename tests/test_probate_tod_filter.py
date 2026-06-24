"""Tests for should_include_probate_row — the Phase 3 living-owner TOD filter.

Pure-function tests (no DB, no network). The worker uses this predicate at the
single persistence chokepoint to decide whether a probate row survives into the
delivered lead set, based on the customer's tri-state opt-in:

  include_living_owner_tod:
    None  -> legacy / grandfathered  -> INCLUDE everything (Phase 2 still labels it)
    False -> new probate default     -> EXCLUDE living-owner TOD planning docs
    True  -> explicit opt-in         -> INCLUDE everything

Only ProbateSignal.TOD_LIVING_OWNER rows are ever dropped, and only when the flag
is exactly False. Death, death-triggered TOD, nonprobate, and unknown rows always
survive — the filter narrows honestly, it never silently widens a death claim.

The predicate classifies via classify_probate_signal_for_row(doc_type, comment) so
a recorder COMMENT can upgrade a bare TOD deed into a death-triggered (real) lead —
those must be KEPT even when the customer excluded living-owner TOD (Codex).
"""

from src.scrapers.probate import (
    effective_tod_on_update,
    new_probate_config_tod_default,
    should_include_probate_row,
)


# --- Non-probate record types are never touched by this filter -------------------
def test_non_probate_record_type_always_included():
    # A pre_foreclosure/tax row is out of scope even if the flag is False and the
    # doc string looks TOD-ish.
    assert should_include_probate_row("pre_foreclosure", False, "Transfer on Death Deed", None) is True


# --- Grandfather: None includes everything ---------------------------------------
def test_none_flag_includes_living_owner_tod():
    assert should_include_probate_row("probate", None, "Transfer on Death Deed", None) is True


def test_none_flag_includes_death():
    assert should_include_probate_row("probate", None, "Certificate of Death", None) is True


# --- Explicit opt-in: True includes everything -----------------------------------
def test_true_flag_includes_living_owner_tod():
    assert should_include_probate_row("probate", True, "Transfer on Death Deed", None) is True


# --- False excludes ONLY living-owner TOD ----------------------------------------
def test_false_flag_drops_plain_tod_deed():
    assert should_include_probate_row("probate", False, "Transfer on Death Deed", None) is False


def test_false_flag_drops_hyphenated_tod():
    assert should_include_probate_row("probate", False, "Transfer-on-Death Deed", None) is False


def test_false_flag_keeps_death_certificate():
    assert should_include_probate_row("probate", False, "Certificate of Death", None) is True


def test_false_flag_keeps_bare_probate():
    # Pierce ARMS emits a bare "PROBATE" -> DEATH_INHERITANCE -> kept.
    assert should_include_probate_row("probate", False, "PROBATE", None) is True


def test_false_flag_keeps_lack_of_probate_affidavit():
    # NONPROBATE_TRANSFER is a deceased-owner title-clearing doc -> kept.
    assert should_include_probate_row("probate", False, "Lack of Probate Affidavit", None) is True


def test_false_flag_keeps_unknown():
    # Only TOD_LIVING_OWNER is dropped; an UNKNOWN signal stays (labeled honestly).
    assert should_include_probate_row("probate", False, "Community Property Agreement", None) is True


# --- The Codex case: a recorder COMMENT upgrades a TOD deed to death-triggered ----
def test_false_flag_keeps_death_triggered_tod_via_comment():
    # doc_type alone is a living-owner TOD, but the comment proves a death event,
    # so classify_probate_signal_for_row returns DEATH_INHERITANCE -> KEEP.
    assert should_include_probate_row(
        "probate", False, "Transfer on Death Deed", "Affidavit of Death of Transferor"
    ) is True


def test_false_flag_drops_tod_with_unrelated_comment():
    # A benign comment that carries no death marker leaves it a living-owner TOD -> drop.
    assert should_include_probate_row(
        "probate", False, "Transfer on Death Deed", "Recorded for grantor"
    ) is False


# --- Empty / missing doc_type is not a TOD -> kept under False --------------------
def test_false_flag_keeps_empty_doc_type():
    assert should_include_probate_row("probate", False, None, None) is True
    assert should_include_probate_row("probate", False, "", None) is True


# --- new_probate_config_tod_default: NEW config default resolution ----------------
def test_new_probate_omitted_defaults_to_exclude():
    # A new probate config with no explicit choice excludes living-owner TOD.
    assert new_probate_config_tod_default("probate", None) is False


def test_new_probate_explicit_true_honored():
    assert new_probate_config_tod_default("probate", True) is True


def test_new_probate_explicit_false_honored():
    assert new_probate_config_tod_default("probate", False) is False


def test_new_non_probate_stays_none():
    # Non-probate configs never get the flag defaulted on.
    assert new_probate_config_tod_default("pre_foreclosure", None) is None
    assert new_probate_config_tod_default("tax_delinquent", None) is None


def test_new_non_probate_passes_through_provided():
    # Pure pass-through for non-probate (route-level validation rejects a stray
    # explicit value separately; the helper itself does not mutate it).
    assert new_probate_config_tod_default("pre_foreclosure", True) is True


# --- effective_tod_on_update: PATCH edit resolution (Codex P2) --------------------
def test_patch_omitted_preserves_stored_false():
    # Editing an unrelated field on a False config keeps it False.
    assert effective_tod_on_update(False, None, False) is False


def test_patch_omitted_preserves_stored_none():
    # Editing an old grandfathered (None) config never flips it to a value.
    assert effective_tod_on_update(False, None, None) is None


def test_patch_explicit_null_does_not_downgrade_false_to_none():
    # The bug: `"include_living_owner_tod": null` (field present, value None) must
    # NOT overwrite a stored False back to grandfathered/include-all.
    assert effective_tod_on_update(True, None, False) is False


def test_patch_explicit_null_keeps_stored_true():
    assert effective_tod_on_update(True, None, True) is True


def test_patch_explicit_true_opts_in():
    assert effective_tod_on_update(True, True, False) is True


def test_patch_explicit_false_opts_out():
    assert effective_tod_on_update(True, False, None) is False
