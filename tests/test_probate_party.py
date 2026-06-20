"""Tests for the shared probate party-orientation helper (src/scrapers/probate.py).

Anchored on the REAL grantor/grantee values captured live during the 21-county
probate audit (2026-06-19): cowlitz/king filing-agency death certs, the okanogan
"Estate of" caption, and the clean counties that must stay unchanged.
"""
from src.scrapers.probate import (
    is_filing_agency_party,
    is_person_like_party,
    orient_probate_party,
    strip_estate_caption,
    strip_filing_agency,
)

# --- strip_filing_agency: the three live agency shapes -------------------------

def test_strip_dept_of_health_with_state_prefix():
    assert strip_filing_agency("STATE OF WASHINGTON DEPARTMENT OF HEALTH") == ""


def test_strip_dept_of_health_state_first():
    assert strip_filing_agency("WASHINGTON STATE DEPARTMENT OF HEALTH") == ""


def test_strip_bare_state_of_washington():
    assert strip_filing_agency("STATE OF WASHINGTON") == ""


def test_strip_agency_concatenated_onto_decedent():
    # EagleWeb/benton shape: decedent + agency in one grantor cell.
    assert strip_filing_agency("PERRIN, RONALD RALPH, STATE OF WA, DEPT OF HEALTH") == (
        "PERRIN, RONALD RALPH"
    )


def test_strip_wa_abbrev_dept_of_health():
    # Codex P2: abbreviated "WA DEPT OF HEALTH" must not leave a lone "WA".
    assert strip_filing_agency("WA DEPT OF HEALTH") == ""
    assert strip_filing_agency("WASH. DEPARTMENT OF HEALTH") == ""


def test_orient_wa_abbrev_agency_promotes_decedent():
    party, heirs = orient_probate_party(
        "WA DEPT OF HEALTH", "PERRY ALICE M", "Death Certificate"
    )
    assert party == "PERRY ALICE M"
    assert heirs is None


# --- strip_filing_agency: real entities that must NOT be stripped -------------

def test_university_not_stripped():
    assert strip_filing_agency("WASHINGTON STATE UNIVERSITY") == "WASHINGTON STATE UNIVERSITY"


def test_person_surname_washington_not_stripped():
    assert strip_filing_agency("WASHINGTON, GEORGE") == "WASHINGTON, GEORGE"


def test_state_farm_not_stripped():
    assert strip_filing_agency("STATE FARM") == "STATE FARM"


def test_ordinary_decedent_unchanged():
    assert strip_filing_agency("LUEDKE ELEANOR MAE") == "LUEDKE ELEANOR MAE"


# --- predicates ---------------------------------------------------------------

def test_is_filing_agency_party_true():
    assert is_filing_agency_party("STATE OF WASHINGTON DEPARTMENT OF HEALTH")
    assert is_filing_agency_party("STATE OF WASHINGTON")


def test_is_filing_agency_party_false_for_person():
    assert not is_filing_agency_party("NELSON MYRNA JOAN")
    assert not is_filing_agency_party("")
    assert not is_filing_agency_party(None)


def test_is_person_like_rejects_agency_and_org():
    assert not is_person_like_party("WASHINGTON STATE DEPARTMENT OF HEALTH")
    assert not is_person_like_party("ACME TITLE COMPANY")
    assert not is_person_like_party("SUPERIOR COURT")
    assert is_person_like_party("CONKLIN DENNIS WILLIAM")


# --- strip_estate_caption -----------------------------------------------------

def test_estate_caption_collapses_to_decedent():
    # okanogan live sample.
    out = strip_estate_caption("ESTATE OF GLENNA K JONES / JONES, GLENNA K / JONES, GLENN I")
    assert out == "GLENNA K JONES"


def test_estate_caption_in_re_form():
    assert strip_estate_caption("IN RE THE ESTATE OF SMITH, JOHN") == "SMITH, JOHN"


def test_no_caption_unchanged():
    assert strip_estate_caption("CONKLIN DENNIS WILLIAM") == "CONKLIN DENNIS WILLIAM"


def test_stacked_multi_grantor_no_caption_preserved():
    # Codex P2: a stacked party with NO caption must keep every co-party.
    assert strip_estate_caption("SMITH JOHN / SMITH JANE") == "SMITH JOHN / SMITH JANE"


def test_orient_stacked_grantor_decedents_preserved():
    party, heirs = orient_probate_party(
        "SMITH JOHN / SMITH JANE", "SMITH HEIR", "DEATH CERTIFICATE"
    )
    assert party == "SMITH JOHN / SMITH JANE"
    assert heirs == "SMITH HEIR"


def test_person_named_with_estate_word_not_corrupted():
    # "ESTATE" only stripped as a leading "ESTATE OF" caption.
    assert strip_estate_caption("ESTATES, MARIA") == "ESTATES, MARIA"


# --- orient_probate_party: the live wrong-party rows get fixed ----------------

def test_orient_cowlitz_dept_of_health_promotes_decedent():
    party, heirs = orient_probate_party(
        "STATE OF WASHINGTON DEPARTMENT OF HEALTH", "LUEDKE ELEANOR MAE", "Death Certificate"
    )
    assert party == "LUEDKE ELEANOR MAE"
    assert heirs is None


def test_orient_cowlitz_bare_state_promotes_decedent():
    party, heirs = orient_probate_party(
        "STATE OF WASHINGTON", "NELSON MYRNA JOAN", "Death Certificate"
    )
    assert party == "NELSON MYRNA JOAN"


def test_orient_king_dept_of_health_promotes_decedent():
    party, _ = orient_probate_party(
        "WASHINGTON STATE DEPARTMENT OF HEALTH", "CONKLIN DENNIS WILLIAM", "DEATH CERTIFICATE"
    )
    assert party == "CONKLIN DENNIS WILLIAM"


def test_orient_okanogan_estate_caption():
    party, _ = orient_probate_party(
        "ESTATE OF GLENNA K JONES / JONES, GLENNA K / JONES, GLENN I",
        "JONES, GLENN I",
        "Personal Representative's Deed",
    )
    assert party == "GLENNA K JONES"


# --- orient_probate_party: guards ---------------------------------------------

def test_orient_normal_grantor_decedent_unchanged():
    # The common clean case (clark/skagit/etc) — grantor IS the decedent.
    party, heirs = orient_probate_party("SMITH, JANE A", "SMITH, JOHN", "DEATH CERTIFICATE")
    assert party == "SMITH, JANE A"
    assert heirs == "SMITH, JOHN"


def test_orient_guard2_both_agency_returns_none():
    party, heirs = orient_probate_party(
        "STATE OF WASHINGTON", "WASHINGTON STATE DEPARTMENT OF HEALTH", "Death Certificate"
    )
    assert party is None
    assert heirs is None


def test_orient_guard1_agency_grantor_empty_grantee():
    party, heirs = orient_probate_party("STATE OF WASHINGTON", "", "Death Certificate")
    assert party is None
    assert heirs is None


def test_orient_guard3_tod_deed_keeps_living_owner():
    # TOD grantor is a LIVING owner — never swap even if a grantee exists.
    party, heirs = orient_probate_party(
        "DOE, JOHN", "DOE FAMILY TRUST", "TRANSFER ON DEATH DEED"
    )
    assert party == "DOE, JOHN"
    assert heirs == "DOE FAMILY TRUST"


def test_orient_no_doc_type_still_safe_on_agency():
    # doc_type omitted: agency swap still fires (agency grantor is never a TOD owner).
    party, _ = orient_probate_party("STATE OF WASHINGTON", "NELSON MYRNA JOAN")
    assert party == "NELSON MYRNA JOAN"


# --- Consolidation extension (2026-06-19): EagleWeb/Skagit absorb ---------------
# Phrase-based agency variants the per-template copies (eagleweb._strip_filing_agency,
# skagit._is_filing_state_party) and audit surfaced. Codex-reviewed: tokens are
# PHRASE-anchored, never bare, so a real "LAST, FIRST" decedent is not false-dropped.

def test_strip_vital_records_tail():
    # Out-of-state cert: decedent + a Bureau/Office of Vital Records/Statistics tail.
    assert strip_filing_agency("DOE, JANE A, BUREAU OF VITAL STATISTICS") == "DOE, JANE A"
    assert strip_filing_agency("DEPARTMENT OF VITAL RECORDS") == ""


def test_strip_dept_of_licensing_and_revenue():
    assert strip_filing_agency("STATE OF WASHINGTON DEPARTMENT OF LICENSING") == ""
    assert strip_filing_agency("SMITH, ROBERT, DEPT OF REVENUE") == "SMITH, ROBERT"


def test_strip_per_segment_bare_state_dropped_keeps_person():
    # Stacked grantor where one " / "-segment is a bare filing state (Codex-endorsed
    # exact-segment drop). The person segment survives.
    assert strip_filing_agency("DOE, JOHN / STATE OF WASHINGTON") == "DOE, JOHN"
    assert strip_filing_agency("WASHINGTON STATE / NELSON MYRNA JOAN") == "NELSON MYRNA JOAN"


def test_strip_per_segment_agency_with_health_dept_dropped():
    # Health-dept phrase stripped in-place reduces a segment to a bare state, then
    # the bare-state segment is dropped, leaving the decedent.
    assert strip_filing_agency(
        "DOE, JOHN / WASHINGTON STATE DEPARTMENT OF HEALTH"
    ) == "DOE, JOHN"


def test_strip_per_segment_keeps_genuine_co_decedents():
    # No agency/state segment present -> every co-party kept (no spurious drop).
    assert strip_filing_agency("SMITH JOHN / SMITH JANE") == "SMITH JOHN / SMITH JANE"


def test_is_person_like_rejects_funeral_and_examiner():
    # A death-cert grantee that is an institution must NOT be promoted to the lead.
    assert not is_person_like_party("EVERGREEN FUNERAL HOME")
    assert not is_person_like_party("SMITH MORTUARY")
    assert not is_person_like_party("PIERCE COUNTY MEDICAL EXAMINER")
    assert not is_person_like_party("KING COUNTY CORONER")
    assert not is_person_like_party("DEPARTMENT OF LICENSING")


def test_is_person_like_still_accepts_real_decedent():
    assert is_person_like_party("CONKLIN DENNIS WILLIAM")
    assert is_person_like_party("PERRIN, RONALD RALPH")


def test_orient_agency_grantor_funeral_home_grantee_dropped():
    # Both sides institutional -> guard #2 returns (None, None); no agency lead leaks.
    party, heirs = orient_probate_party(
        "WASHINGTON STATE DEPARTMENT OF HEALTH", "EVERGREEN FUNERAL HOME", "Death Certificate"
    )
    assert party is None
    assert heirs is None


def test_orient_per_segment_agency_co_party_stripped():
    party, heirs = orient_probate_party(
        "DOE, JOHN / STATE OF WASHINGTON", "DOE, JANE", "DEATH CERTIFICATE"
    )
    assert party == "DOE, JOHN"
    assert heirs == "DOE, JANE"
