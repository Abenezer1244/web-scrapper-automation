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


# --- Codex review fixes (round 2) ---------------------------------------------

def test_bare_state_only_matches_real_states_not_any_word():
    # Codex P2: the per-segment drop must NOT drop a co-party that merely ends in
    # "STATE" but is not an actual US state ("MCKINLEY STATE", "JOHN STATE").
    assert strip_filing_agency("DOE, JOHN / MCKINLEY STATE") == "DOE, JOHN / MCKINLEY STATE"
    assert strip_filing_agency("MCKINLEY STATE") == "MCKINLEY STATE"
    assert strip_filing_agency("JOHN STATE / SMITH JANE") == "JOHN STATE / SMITH JANE"


def test_bare_state_still_matches_real_states_both_orders():
    assert strip_filing_agency("STATE OF WASHINGTON") == ""
    assert strip_filing_agency("WASHINGTON STATE") == ""
    assert strip_filing_agency("WASH. STATE OF") == ""        # Skagit inverted
    assert strip_filing_agency("STATE OF OREGON") == ""       # out-of-state
    assert strip_filing_agency("CALIFORNIA STATE OF") == ""   # Skagit inverted, other state


def test_concatenated_washington_state_word_order_dept_health_stripped():
    # Codex P2: "<decedent>, WASHINGTON STATE DEPARTMENT OF HEALTH" (state-word
    # order, not "STATE OF WA") must strip the FULL agency, not leave "WASHINGTON
    # STATE" polluting the decedent.
    assert strip_filing_agency(
        "PERRIN, RONALD, WASHINGTON STATE DEPARTMENT OF HEALTH"
    ) == "PERRIN, RONALD"
    assert strip_filing_agency("WASHINGTON STATE DEPARTMENT OF HEALTH") == ""


def test_is_person_like_comma_form_rescues_institution_word_surname():
    # Codex P2: a real decedent whose surname collides with an institution word
    # ("CORONER, JANE", "BANK, JOHN") is still person-like in LAST, FIRST form.
    assert is_person_like_party("CORONER, JANE")
    assert is_person_like_party("BANK, JOHN")


def test_is_person_like_still_rejects_institutional_form():
    # The institution form (NOT comma LAST, FIRST) is still rejected.
    assert not is_person_like_party("PIERCE COUNTY CORONER")
    assert not is_person_like_party("FIRST NATIONAL BANK")
    assert not is_person_like_party("WASHINGTON, STATE OF")  # comma-inverted state


# --- Test 7 audit (2026-09-03): King recorder placeholder + agency word orders --
#
# Every input below is a VERBATIM grantor/grantee pair captured from King County's
# live LandmarkWeb Death Certificate index for 06/04/2026-09/02/2026. These assert
# the SEMANTIC outcome (the decedent reaches party_name, the placeholder reaches
# nothing) — not merely that some string is forbidden.

def test_placeholder_grantor_promotes_the_decedent_grantee():
    # King indexed instrument 20260828001142 with the parties reversed: the
    # recorder placeholder as grantor, the decedent as grantee. Corroborated at the
    # King Assessor — parcel 3276080220's owner is "TRUJILLO CHUCK+PATSY".
    assert orient_probate_party("PUBLIC", "TRUJILLO CHARLES JAMES", "DEATH CERTIFICATE") == (
        "TRUJILLO CHARLES JAMES", None
    )
    # Same defect, instrument 20260710000167 (assessor owner MCINTOSH LEONA LORRAINE).
    assert orient_probate_party("PUBLIC", "MCINTOSH JOHN HAROLD", "DEATH CERTIFICATE") == (
        "MCINTOSH JOHN HAROLD", None
    )


def test_placeholder_grantee_is_not_an_heir():
    # The dominant live shape: real decedent as grantor, placeholder as grantee
    # (101 of 204 rows). The decedent must survive untouched and heirs must be None
    # rather than the literal placeholder.
    for placeholder in ("PUBLIC", "THE PUBLIC", "PUBLIC THE"):
        assert orient_probate_party(
            "ELTING EMILY WILLIAMS", placeholder, "DEATH CERTIFICATE"
        ) == ("ELTING EMILY WILLIAMS", None)


def test_placeholder_dropped_from_a_stacked_grantee_keeping_the_real_heir():
    assert orient_probate_party(
        "SINGH GURDEV", "KAUR RAJWANT / PUBLIC", "DEATH CERTIFICATE"
    ) == ("SINGH GURDEV", "KAUR RAJWANT")


def test_placeholder_never_promoted_into_party_name():
    # Both sides non-parties -> no lead party at all (guard #2), never "PUBLIC".
    assert orient_probate_party(
        "WASHINGTON STATE DEPT OF HEALTH", "PUBLIC", "DEATH CERTIFICATE"
    ) == (None, None)


def test_placeholder_rule_does_not_touch_real_public_entities_or_people():
    # Whole-value anchoring: these must all pass through untouched.
    for value in (
        "PUBLIC STORAGE",
        "PUBLIC UTILITY DISTRICT NO 1",
        "REPUBLIC SERVICES",
        "PUBLIC, JOHN",
        "PUBLICOVER MARGARET",
    ):
        assert strip_filing_agency(value) == value
    assert is_person_like_party("PUBLIC, JOHN")


def test_agency_trailing_word_order_promotes_the_decedent():
    # Instrument 20260715000926 — "<STATE> HEALTH DEPARTMENT" slipped past the
    # "DEPT OF HEALTH"-only regex and shipped as the lead's party_name.
    assert orient_probate_party(
        "WASHINGTON STATE HEALTH DEPARTMENT", "REINKE NORMAN LEONARD", "DEATH CERTIFICATE"
    ) == ("REINKE NORMAN LEONARD", None)


def test_agency_scrambled_word_order_promotes_the_decedent():
    # Instrument 20260626000676 — "DEPARTMENT <STATE> HEALTH".
    assert orient_probate_party(
        "DEPARTMENT WASHINGTON STATE HEALTH", "MICHALENKO TANITA C", "DEATH CERTIFICATE"
    ) == ("MICHALENKO TANITA C", None)


def test_bare_state_with_govt_marker_promotes_the_decedent():
    # Instruments 20260612000387/388 — "WASHINGTON STATE-GOVT" (and the unhyphenated
    # form seen in the grantee slot).
    assert orient_probate_party(
        "WASHINGTON STATE-GOVT", "LAROUX JOHN ALEXANDER", "DEATH CERTIFICATE"
    ) == ("LAROUX JOHN ALEXANDER", None)
    assert strip_filing_agency("WASHINGTON STATE GOVT") == ""


def test_new_agency_orders_do_not_strip_real_names():
    # The trailing/scrambled regexes require a DEPARTMENT/DEPT token adjacent to a
    # vital-records subject, so these real values must survive intact.
    for value in (
        "WASHINGTON STATE UNIVERSITY",
        "HEALTH JOHN ROBERT",
        "STATE FARM",
        "WASHINGTON, GEORGE",
        "GOVT MARIA",
        "DEPARTMENT, ANNA",
    ):
        assert strip_filing_agency(value) == value


def test_agency_grantee_is_not_an_heir_when_grantor_is_the_decedent():
    # The mirror-image of the promotion case: 5 live rows carry the agency in the
    # GRANTEE slot while the grantor is already the decedent. heirs must be None.
    for agency in (
        "WASHINGTON STATE DEPARTMENT OF HEALTH",
        "WASHINGTON STATE OF DEPARTMENT OF HEALTH",
        "WASHINGTON STATE DEPT OF HEALTH",
        "STATE OF WASHINGTON DEPARTMENT OF HEALTH",
        "WASHINGTON STATE-GOVT",
    ):
        assert orient_probate_party("SERONKO ROBERT LEE", agency, "DEATH CERTIFICATE") == (
            "SERONKO ROBERT LEE", None
        )


def test_counterparty_cleanup_does_not_fire_on_a_transfer_on_death_deed():
    # Guard #3 still holds: a TOD grantor is a LIVING owner and is never swapped,
    # but the grantee slot is still sanitized.
    assert orient_probate_party(
        "NELSON MYRNA JOAN", "PUBLIC", "TRANSFER ON DEATH DEED"
    ) == ("NELSON MYRNA JOAN", None)
    assert orient_probate_party(
        "NELSON MYRNA JOAN", "NELSON DAVID", "TRANSFER ON DEATH DEED"
    ) == ("NELSON MYRNA JOAN", "NELSON DAVID")


def test_ordinary_king_rows_are_unchanged():
    # 196 of the 204 live rows must pass through with the grantor as party_name.
    assert orient_probate_party(
        "BANEZ MATILDE UMIPIG", "BANEZ JOSELITO U", "DEATH CERTIFICATE"
    ) == ("BANEZ MATILDE UMIPIG", "BANEZ JOSELITO U")
    assert orient_probate_party(
        "THOMAS GARY / THOMAS DELORES A", "TUSZYNSKI GARY / THOMAS GARY", "DEATH CERTIFICATE"
    ) == ("THOMAS GARY / THOMAS DELORES A", "TUSZYNSKI GARY / THOMAS GARY")
