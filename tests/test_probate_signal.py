"""Tests for classify_probate_signal — the probate lead-subtype classifier.

Pure-function tests (no DB, no network). Verifies that a recorder/court document
type string is sorted into the correct probate signal bucket so the pipeline can
label every probate lead honestly:

  - probate_death_inheritance     -> a real death/inheritance event (the clean lead)
  - tod_living_owner_estate_planning -> a LIVING owner's Transfer-on-Death planning
                                        doc (a softer signal, must never be sold as
                                        a death)
  - nonprobate_transfer           -> Lack-of-Probate affidavit (deceased-owner title
                                        clearing without formal probate)
  - unknown_probate               -> no confident signal (e.g. Community Property
                                        Agreement) — honestly labeled, not faked

Strings below are REAL doc_type values observed in the 2026-06-23 live verification
(some carry a "\\n<recording#>" suffix the classifier must normalize away), plus the
Codex-supplied edge cases (revocation/amendment of a TOD, death-triggered TOD
effectuations).
"""

from src.scrapers.probate import (
    ProbateSignal,
    classify_probate_signal,
    classify_probate_signal_for_row,
)


# --- Living-owner TOD creation/admin -> tod_living_owner_estate_planning ----------
def test_plain_tod_deed_is_living_owner():
    assert classify_probate_signal("Transfer on Death Deed") is ProbateSignal.TOD_LIVING_OWNER


def test_uppercase_tod_deed_is_living_owner():
    assert classify_probate_signal("TRANSFER ON DEATH DEED") is ProbateSignal.TOD_LIVING_OWNER


def test_hyphenated_transfer_on_death_is_living_owner():
    # "Transfer-on-Death Deed" is a common recorder spelling (Codex P2 root-cause).
    assert classify_probate_signal("Transfer-on-Death Deed") is ProbateSignal.TOD_LIVING_OWNER


def test_revocable_tod_is_living_owner():
    # A revocable TOD is still a creation by a living owner.
    assert classify_probate_signal("Revocable Transfer on Death Deed") is ProbateSignal.TOD_LIVING_OWNER


def test_revocation_of_tod_is_living_owner():
    # Revoking a TOD is living-owner estate-planning admin, not a death.
    assert classify_probate_signal("Revocation of Transfer on Death Deed") is ProbateSignal.TOD_LIVING_OWNER


def test_amendment_of_tod_is_living_owner():
    assert classify_probate_signal("Amendment of Transfer on Death Deed") is ProbateSignal.TOD_LIVING_OWNER


def test_bare_tod_abbreviation_is_living_owner():
    assert classify_probate_signal("TOD") is ProbateSignal.TOD_LIVING_OWNER


def test_tod_with_recording_number_suffix_normalized():
    # Live strings concatenate the recording number after a newline.
    assert classify_probate_signal("Transfer on Death Deed\n2026-1482913") is ProbateSignal.TOD_LIVING_OWNER


# --- Death-TRIGGERED TOD effectuation -> probate_death_inheritance (KEEP) ---------
def test_death_cert_to_perfect_death_deed_is_death():
    # cowlitz live string — a death actually happened; this is a real lead.
    assert classify_probate_signal("Death Certificate to Perfect Death Deed") is ProbateSignal.DEATH_INHERITANCE


def test_affidavit_to_perfect_tod_is_death():
    assert classify_probate_signal("Affidavit to Perfect Transfer on Death Deed") is ProbateSignal.DEATH_INHERITANCE


def test_tod_beneficiary_affidavit_is_death():
    assert classify_probate_signal("Transfer on Death Deed Beneficiary Affidavit") is ProbateSignal.DEATH_INHERITANCE


def test_affidavit_of_death_of_transferor_is_death():
    assert classify_probate_signal("Affidavit of Death of Transferor") is ProbateSignal.DEATH_INHERITANCE


# --- Genuine death / probate docs -> probate_death_inheritance --------------------
def test_death_certificate_is_death():
    assert classify_probate_signal("DEATH CERTIFICATE") is ProbateSignal.DEATH_INHERITANCE


def test_certificate_of_death_is_death():
    assert classify_probate_signal("Certificate Of Death\n4600099") is ProbateSignal.DEATH_INHERITANCE


def test_letters_testamentary_is_death():
    assert classify_probate_signal("Letters Testamentary") is ProbateSignal.DEATH_INHERITANCE


def test_letters_of_administration_is_death():
    assert classify_probate_signal("Letters of Administration") is ProbateSignal.DEATH_INHERITANCE


def test_personal_representative_deed_is_death():
    assert classify_probate_signal("Personal Representative Deed\n4599799") is ProbateSignal.DEATH_INHERITANCE


def test_administrators_deed_is_death():
    assert classify_probate_signal("Administrator's Deed") is ProbateSignal.DEATH_INHERITANCE


def test_affidavit_of_heirship_is_death():
    assert classify_probate_signal("Affidavit of Heirship") is ProbateSignal.DEATH_INHERITANCE


def test_bare_probate_is_death():
    # Pierce ARMS emits a generic "PROBATE" doc_type; live data shows real
    # estate/heir parties with parcels behind it.
    assert classify_probate_signal("PROBATE") is ProbateSignal.DEATH_INHERITANCE


def test_abbreviated_death_codes_are_death():
    # Codex P2: EagleWeb (Clallam) + AcclaimWeb (Chelan) emit short whole-value codes.
    for code in ("DEATH", "LETTR", "EXEC", "SUCC", "AFFD", "PTREC", "WILL", "HEIR"):
        assert classify_probate_signal(code) is ProbateSignal.DEATH_INHERITANCE, code
        # case-insensitive normalization
        assert classify_probate_signal(code.title()) is ProbateSignal.DEATH_INHERITANCE, code


def test_will_variants_are_death():
    # Codex P2: Clark/Whatcom emit multi-word/punctuated Will labels; the exact-value
    # code set only caught a bare WILL, so the marker regex must handle the variants.
    for label in ("WILL", "Will", "LAST WILL AND TESTAMENT", "WILL/TESTAMENT",
                  "Last Will & Testament", "Will and Testament"):
        assert classify_probate_signal(label) is ProbateSignal.DEATH_INHERITANCE, label


def test_heir_variants_are_death():
    for label in ("HEIR", "HEIRS", "Affidavit of Heirs"):
        assert classify_probate_signal(label) is ProbateSignal.DEATH_INHERITANCE, label


def test_bare_death_code_does_not_leak_into_tod_label():
    # The exact-value "DEATH" code must NOT make a multi-word TOD deed look like a
    # death — "Transfer on Death Deed" stays a living-owner signal.
    assert classify_probate_signal("Transfer on Death Deed") is ProbateSignal.TOD_LIVING_OWNER
    # ...and a bare "TOD" code stays living-owner (it is not in the death-code set).
    assert classify_probate_signal("TOD") is ProbateSignal.TOD_LIVING_OWNER


# --- Lack of Probate affidavit -> nonprobate_transfer ----------------------------
def test_lack_of_probate_affidavit_is_nonprobate_transfer():
    assert classify_probate_signal("LACK OF PROBATE AFFIDAVIT") is ProbateSignal.NONPROBATE_TRANSFER


def test_affidavit_lack_of_probate_is_nonprobate_transfer():
    assert classify_probate_signal("Affidavit Lack of Probate") is ProbateSignal.NONPROBATE_TRANSFER


def test_hyphenated_lack_of_probate_is_nonprobate_transfer():
    # Codex P2: recorders commonly hyphenate; the value must NOT fall through to the
    # bare-PROBATE rule and get mislabeled as a death/inheritance lead.
    assert classify_probate_signal("Lack-of-Probate Affidavit") is ProbateSignal.NONPROBATE_TRANSFER
    assert classify_probate_signal("Affidavit of Lack-of-Probate") is ProbateSignal.NONPROBATE_TRANSFER


# --- Unknown / out-of-scope -> unknown_probate (honest, not faked) ---------------
def test_community_property_agreement_is_unknown():
    # Living spouses' estate planning — deferred to a follow-up; must NOT be labeled
    # a death, but also not silently treated as TOD. Honest "unknown".
    assert classify_probate_signal("Community Property Agreement") is ProbateSignal.UNKNOWN


def test_empty_is_unknown():
    assert classify_probate_signal("") is ProbateSignal.UNKNOWN
    assert classify_probate_signal(None) is ProbateSignal.UNKNOWN


def test_whitespace_only_is_unknown():
    assert classify_probate_signal("   \n  ") is ProbateSignal.UNKNOWN


# --- Subtype values are the stable strings the pipeline exports -------------------
def test_inheritance_marker_is_death():
    # Codex P2: Skagit keeps probate rows whose comment is "INHERITANCE".
    assert classify_probate_signal("INHERITANCE") is ProbateSignal.DEATH_INHERITANCE
    assert classify_probate_signal_for_row(
        "Affidavit", comment="INHERITANCE"
    ) is ProbateSignal.DEATH_INHERITANCE


def test_comment_fallback_when_doctype_unknown():
    # Codex P2: Skagit keeps a generic "Affidavit" row whose probate signal lives in
    # the recorder comment. doc_type alone is UNKNOWN -> the comment decides.
    assert classify_probate_signal("Affidavit") is ProbateSignal.UNKNOWN
    assert classify_probate_signal_for_row(
        "Affidavit", comment="LACK OF PROBATE AFFIDAVIT"
    ) is ProbateSignal.NONPROBATE_TRANSFER


def test_comment_never_downgrades_a_confident_doctype():
    # A death doc_type stays death even if the comment looks like a living-owner TOD —
    # the STRONGER signal wins (death > tod), the comment can't downgrade it.
    assert classify_probate_signal_for_row(
        "DEATH CERTIFICATE", comment="Transfer on Death Deed"
    ) is ProbateSignal.DEATH_INHERITANCE


def test_death_triggered_tod_via_comment_is_death():
    # Codex P2: TOD doc_type whose DEATH marker lives in the comment is a real
    # death-triggered effectuation — the comment must UPGRADE tod -> death.
    assert classify_probate_signal_for_row(
        "Transfer on Death Deed", comment="Affidavit of Death"
    ) is ProbateSignal.DEATH_INHERITANCE
    assert classify_probate_signal_for_row(
        "Transfer on Death Deed", comment="Beneficiary Affidavit"
    ) is ProbateSignal.DEATH_INHERITANCE
    # ...but a plain TOD with no death marker anywhere stays living-owner.
    assert classify_probate_signal_for_row(
        "Transfer on Death Deed", comment="recorded per request"
    ) is ProbateSignal.TOD_LIVING_OWNER


def test_no_comment_falls_back_to_doctype_result():
    assert classify_probate_signal_for_row("Transfer on Death Deed", None) is ProbateSignal.TOD_LIVING_OWNER
    assert classify_probate_signal_for_row("Affidavit", None) is ProbateSignal.UNKNOWN


def test_signal_values_are_stable_strings():
    assert ProbateSignal.DEATH_INHERITANCE.value == "probate_death_inheritance"
    assert ProbateSignal.TOD_LIVING_OWNER.value == "tod_living_owner_estate_planning"
    assert ProbateSignal.NONPROBATE_TRANSFER.value == "nonprobate_transfer"
    assert ProbateSignal.UNKNOWN.value == "unknown_probate"
