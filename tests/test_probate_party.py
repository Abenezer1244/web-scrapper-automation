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
