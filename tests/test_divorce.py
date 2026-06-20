"""Tests for src/scrapers/divorce.py — the shared divorce classifier + party guard.

Deterministic, fixture-driven (real recorder document-type labels). No mocks: the
functions are pure string logic, exercised with realistic inputs.
"""
import pytest

from src.scrapers.divorce import (
    DivorceMatch,
    classify_divorce_doc,
    is_divorce_doc,
    orient_divorce_party,
)

# ── classify_divorce_doc: unambiguous marital positives -> MATCH ──────────────

MATCH_DOCS = [
    "DIVORCE",
    "Decree-divorce",                         # Skagit server doc-type value
    "DECREE OF DISSOLUTION",                  # Pierce ARMS checkbox 87 label
    "DECREE OF DISSOLUTION OF MARRIAGE",
    "DISSOLUTION OF MARRIAGE",
    "MARRIAGE DISSOLUTION",
    "Dissolution of Domestic Partnership",    # WA family-law positive
    "LEGAL SEPARATION",
    "DECREE OF LEGAL SEPARATION",
    "decree of dissolution",                  # case-insensitive
]


@pytest.mark.parametrize("doc", MATCH_DOCS)
def test_strong_positives_are_match(doc):
    assert classify_divorce_doc(doc) is DivorceMatch.MATCH
    # MATCH is kept regardless of precise_source.
    assert is_divorce_doc(doc) is True
    assert is_divorce_doc(doc, precise_source=True) is True


# ── classify_divorce_doc: explicit non-marital -> NON_MATCH ───────────────────

NON_MATCH_DOCS = [
    "ARTICLES OF DISSOLUTION",
    "CERTIFICATE OF DISSOLUTION",
    "STATEMENT OF DISSOLUTION",
    "DISSOLUTION OF PARTNERSHIP",
    "DISSOLUTION OF NONPROFIT CORPORATION",
    "DISSOLUTION OF LIMITED LIABILITY COMPANY",
    "LLC DISSOLUTION",
    "ACME CORP DISSOLUTION",
    "CORPORATE DISSOLUTION",
    "SEPARATION",                 # bare separation (user excluded)
    "SEPARATION AGREEMENT",
    "LEGAL SEPARATION AGREEMENT",  # agreement, not a decree (Codex: order matters)
    "PROPERTY SETTLEMENT AGREEMENT",
    "WARRANTY DEED",              # no divorce signal at all
    "DEED OF TRUST",
    "",
    None,
]


@pytest.mark.parametrize("doc", NON_MATCH_DOCS)
def test_negatives_are_non_match(doc):
    assert classify_divorce_doc(doc) is DivorceMatch.NON_MATCH
    # Never kept, even from a precise server-side source.
    assert is_divorce_doc(doc) is False
    assert is_divorce_doc(doc, precise_source=True) is False


# ── classify_divorce_doc: ambiguous bare DISSOLUTION/DECREE ───────────────────

AMBIGUOUS_DOCS = [
    "DISSOLUTION",
    "DECREE",
    "FINAL DECREE",
    "DISS",          # EagleWeb dissolution abbreviation (Codex review)
    "DISOL",         # EagleWeb dissolution abbreviation (Codex review)
]


@pytest.mark.parametrize("doc", AMBIGUOUS_DOCS)
def test_ambiguous_classification(doc):
    assert classify_divorce_doc(doc) is DivorceMatch.AMBIGUOUS


@pytest.mark.parametrize("doc", AMBIGUOUS_DOCS)
def test_ambiguous_fails_closed_without_precise_source(doc):
    # Generic keyword-only connector -> drop the ambiguous bucket.
    assert is_divorce_doc(doc) is False
    assert is_divorce_doc(doc, precise_source=False) is False


@pytest.mark.parametrize("doc", AMBIGUOUS_DOCS)
def test_ambiguous_kept_with_precise_source(doc):
    # Pierce/Skagit already constrained the server-side search to divorce.
    assert is_divorce_doc(doc, precise_source=True) is True


def test_domestic_partnership_positive_beats_entity_token():
    # "PARTNERSHIP" is an entity token, but the marital phrase wins (checked first).
    assert classify_divorce_doc("DISSOLUTION OF DOMESTIC PARTNERSHIP") is DivorceMatch.MATCH
    assert is_divorce_doc("DISSOLUTION OF DOMESTIC PARTNERSHIP") is True


def test_business_partnership_dissolution_is_rejected():
    assert classify_divorce_doc("DISSOLUTION OF PARTNERSHIP") is DivorceMatch.NON_MATCH


def test_entity_token_word_boundary_does_not_false_fire():
    # "CORP" must not match inside "CORPORAL": the doc still carries the bare
    # "DECREE" marker, so it classifies AMBIGUOUS (not wrongly NON_MATCH'd by a
    # stray entity-substring hit).
    assert classify_divorce_doc("DECREE RE CORPORAL PUNISHMENT") is DivorceMatch.AMBIGUOUS
    # A legit divorce decree is never clobbered by entity-token logic.
    assert classify_divorce_doc("DECREE OF DISSOLUTION") is DivorceMatch.MATCH


# ── orient_divorce_party ──────────────────────────────────────────────────────

def test_orient_person_grantor_is_noop():
    assert orient_divorce_party("DOE, JOHN", "DOE, JANE") == ("DOE, JOHN", "DOE, JANE")


def test_orient_promotes_person_over_state_agency():
    # DSHS / child-support dissolution: recorder put the state in the grantor slot.
    assert orient_divorce_party("STATE OF WASHINGTON", "DOE, JANE") == ("DOE, JANE", None)


def test_orient_promotes_person_over_court():
    assert orient_divorce_party("SUPERIOR COURT OF WASHINGTON", "SMITH, MARY") == (
        "SMITH, MARY",
        None,
    )


def test_orient_no_swap_when_both_persons():
    # Never reorder two real spouses — both are valid leads.
    assert orient_divorce_party("ANDERSON, KARL", "ANDERSON, LISA") == (
        "ANDERSON, KARL",
        "ANDERSON, LISA",
    )


def test_orient_handles_empty_grantee():
    assert orient_divorce_party("DOE, JOHN", "") == ("DOE, JOHN", None)


def test_orient_does_not_promote_non_person_grantee():
    # Grantor non-person AND grantee non-person -> no fabricated swap (no-op).
    party, heirs = orient_divorce_party("STATE OF WASHINGTON", "SUPERIOR COURT")
    assert party == "STATE OF WASHINGTON"
    assert heirs == "SUPERIOR COURT"
