"""King malformed-PID parcel repair (src/scrapers/enrichment/king_parcel_repair.py).

Every parcel number, owner string and party name below is VERBATIM live data
captured from King County on 2026-09-03 while auditing "Test 7". The resolver
decides WHICH property a lead points at, so these assert the semantic outcome and
— more importantly — that it ABSTAINS whenever the evidence is not conclusive.
"""
from src.scrapers.enrichment.king_parcel_repair import (
    candidate_pins,
    candidates_are_exhaustive,
    is_well_formed_king_pin,
    owner_matches_party,
    person_tokens,
    resolve_king_parcel,
)

# The live case: recorder printed 64116000027 for a REINKE death certificate.
# Three of the seven deletion candidates are real King parcels.
_REAL = {
    "6411600002": "SNYDER JACOB",
    "6411600007": "ENTROP RICHARD+SHARYN R",
    "6411600027": "REINKE NORMAN L",
}


def _gis(existing):
    return lambda cands: {c for c in cands if c in existing}


def _owner(table):
    return lambda pin: table.get(pin)


# ── PIN shape ────────────────────────────────────────────────────────────────

def test_well_formed_king_pin():
    assert is_well_formed_king_pin("6411600027")
    assert not is_well_formed_king_pin("64116000027")   # 11 digits
    assert not is_well_formed_king_pin("641160002")     # 9
    assert not is_well_formed_king_pin("641160-0027")   # punctuation is not a PIN
    assert not is_well_formed_king_pin(None)


def test_eleven_digit_candidates_are_every_single_deletion():
    cands = candidate_pins("64116000027")
    assert all(len(c) == 10 for c in cands)
    assert "6411600027" in cands      # the true parcel
    assert "6411600002" in cands      # the one eRealProperty truncates to
    assert "6411600007" in cands
    assert len(cands) == 7            # distinct results, duplicates collapsed
    assert candidates_are_exhaustive("64116000027")


def test_twelve_digit_candidates_stay_bounded_and_are_not_exhaustive():
    # Not every two-deletion combination — that would inflate the survivor count
    # and make "exactly one" meaningless. Because it is bounded, a lone survivor
    # there is not proof on its own.
    assert candidate_pins("012603938700") == sorted({"0126039387", "2603938700", "1260393870"})
    assert len(candidate_pins("259900081003")) == 3
    assert not candidates_are_exhaustive("012603938700")


def test_unmodelled_shapes_yield_no_candidates():
    for value in ("6411600027", "641160", "", None, "641160000271234"):
        assert candidate_pins(value) == []


# ── name matching ────────────────────────────────────────────────────────────

def test_person_tokens_parses_the_live_owner_and_party_forms():
    assert person_tokens("REINKE NORMAN L") == [("REINKE", "NORMAN", "L")]
    assert person_tokens("REINKE NORMAN LEONARD") == [("REINKE", "NORMAN", "LEONARD")]
    assert person_tokens("SMITH, JANE A") == [("SMITH", "JANE", "A")]
    assert ("TRUJILLO", "CHUCK") in person_tokens("TRUJILLO CHUCK+PATSY")
    assert person_tokens("BASS GEORGANNA K / WARREN GEORGANNA K") == [
        ("BASS", "GEORGANNA", "K"), ("WARREN", "GEORGANNA", "K")]


def test_person_tokens_ignores_organisations():
    for entity in ("ACME PROPERTIES LLC", "SMITH FAMILY TRUST", "FIRST NATIONAL BANK",
                   "WASHINGTON STATE DEPARTMENT OF HEALTH", "ESTATE OF JONES MARY"):
        assert person_tokens(entity) == []


def test_owner_match_compares_whole_names():
    # The live match: a middle initial vs a full middle name still matches.
    assert owner_matches_party("REINKE NORMAN L", "REINKE NORMAN LEONARD")
    assert owner_matches_party("REINKE NORMAN", "REINKE NORMAN LEONARD")
    # ...and everything weaker is rejected.
    assert not owner_matches_party("REINKE SANDRA J", "REINKE NORMAN LEONARD")  # surname only
    assert not owner_matches_party("REINKE N L", "REINKE NORMAN LEONARD")       # initial only
    assert not owner_matches_party("ROINSTAD ERIC R", "RONSTAD ERIC R")         # fuzzy spelling
    assert not owner_matches_party("SNYDER JACOB", "REINKE NORMAN LEONARD")
    assert not owner_matches_party("REINKE FAMILY TRUST", "REINKE NORMAN LEONARD")
    assert not owner_matches_party(None, "REINKE NORMAN LEONARD")
    assert not owner_matches_party("REINKE NORMAN L", None)


def test_a_two_token_surname_does_not_collapse_two_different_people():
    # Codex P1: a (surname, first) PAIR reduced both "VAN DYKE MARY" and
    # "VAN DYKE JOHN" to ("VAN", "DYKE"), so two DIFFERENT people matched and the
    # tie-breaker could hand a lead its neighbour's parcel.
    assert not owner_matches_party("VAN DYKE MARY", "VAN DYKE JOHN")
    assert not owner_matches_party("DE LA CRUZ MARIA", "DE LA CRUZ JOSE")
    assert not owner_matches_party("ST JOHN ANNA", "ST JOHN PETER")
    # The same real person still matches across two spellings of the middle name.
    assert owner_matches_party("VAN DYKE MARY E", "VAN DYKE MARY ELLEN")


def test_a_co_owner_half_without_a_surname_cannot_match_a_different_person():
    # "ENTROP RICHARD+SHARYN R" — the second half names no surname, so it must not
    # become evidence about somebody else.
    assert not owner_matches_party("ENTROP RICHARD+SHARYN R", "SHARYN ROBERTS")
    assert not owner_matches_party("ENTROP RICHARD+SHARYN R", "REINKE NORMAN LEONARD")
    assert owner_matches_party("ENTROP RICHARD+SHARYN R", "ENTROP RICHARD")


# ── resolution ───────────────────────────────────────────────────────────────

def test_resolves_the_live_reinke_parcel_by_owner_match():
    stats: dict = {}
    got = resolve_king_parcel(
        "64116000027", "REINKE NORMAN LEONARD",
        gis_exists=_gis(_REAL), owner_of=_owner(_REAL), stats=stats,
    )
    assert got is not None
    assert got.parcel_id == "6411600027"
    assert got.method == "gis_plus_owner_match"
    assert got.candidates_in_gis == ("6411600002", "6411600007", "6411600027")
    assert got.matched_owner == "REINKE NORMAN L"
    assert stats["resolved_owner_match"] == 1
    prov = got.provenance("64116000027")
    assert prov["resolved_parcel_id"] == "6411600027"
    assert prov["source_parcel_id"] == "64116000027"
    assert prov["resolved_party_match"] == "REINKE NORMAN LEONARD"


def test_resolves_a_single_surviving_candidate_from_an_exhaustive_space():
    # 11 digits: every single deletion was enumerated, so a lone real parcel is
    # proof on its own and no owner lookup is consulted.
    def _explode(_pin):
        raise AssertionError("owner lookup must not run for an exhaustive singleton")

    got = resolve_king_parcel(
        "64116000027", "REINKE NORMAN LEONARD",
        gis_exists=_gis({"6411600027": "REINKE NORMAN L"}), owner_of=_explode,
    )
    assert got is not None
    assert got.parcel_id == "6411600027"
    assert got.method == "gis_singleton"


def test_a_singleton_from_a_bounded_space_still_needs_owner_corroboration():
    # Codex P1: the 12-digit candidate set is a guess at the defect shape, so a
    # lone survivor could be the truncation parcel while the real one needed a
    # deletion the set never generated. Existence alone is not proof there.
    assert resolve_king_parcel(
        "012603938700", "RONSTAD ERIC R",
        gis_exists=_gis({"0126039387": "ROINSTAD ERIC R"}),
        owner_of=_owner({"0126039387": "ROINSTAD ERIC R"}),
    ) is None      # ROINSTAD vs RONSTAD is a fuzzy spelling, not evidence
    got = resolve_king_parcel(
        "012603938700", "ROINSTAD ERIC R",
        gis_exists=_gis({"0126039387": "ROINSTAD ERIC R"}),
        owner_of=_owner({"0126039387": "ROINSTAD ERIC R"}),
    )
    assert got is not None and got.method == "gis_plus_owner_match"


def test_abstains_when_several_survive_and_no_owner_matches():
    # Assessor lag: title has transferred, so the decedent matches nobody. A miss
    # is the safe outcome — never a guess.
    stats: dict = {}
    assert resolve_king_parcel(
        "64116000027", "LAROUX JOHN ALEXANDER",
        gis_exists=_gis(_REAL), owner_of=_owner(_REAL), stats=stats,
    ) is None
    assert stats["multi_survivor_no_owner_match"] == 1


def test_abstains_when_two_candidates_match_the_party():
    twins = {"6411600002": "REINKE NORMAN L", "6411600027": "REINKE NORMAN LEONARD"}
    assert resolve_king_parcel(
        "64116000027", "REINKE NORMAN LEONARD",
        gis_exists=_gis(twins), owner_of=_owner(twins),
    ) is None


def test_abstains_when_no_candidate_exists_or_gis_is_down():
    # An empty GIS answer is ALSO what an outage looks like. Aborting on zero is
    # what makes a transient failure safe: it can cost a repair, never buy a
    # wrong one.
    stats: dict = {}
    assert resolve_king_parcel(
        "64116000027", "REINKE NORMAN LEONARD",
        gis_exists=lambda _c: set(), owner_of=_owner(_REAL), stats=stats,
    ) is None
    assert stats["no_candidate_in_gis"] == 1


def test_never_fires_on_a_well_formed_pin():
    stats: dict = {}
    assert resolve_king_parcel(
        "6411600027", "REINKE NORMAN LEONARD",
        gis_exists=_gis(_REAL), owner_of=_owner(_REAL), stats=stats,
    ) is None
    assert stats["skipped_well_formed"] == 1


def test_never_resolves_to_the_parcel_erealproperty_would_have_truncated_to():
    # The whole point: first-10 truncation lands on 6411600002 (SNYDER JACOB).
    # The resolver must not reproduce that answer for a REINKE lead.
    got = resolve_king_parcel(
        "64116000027", "REINKE NORMAN LEONARD",
        gis_exists=_gis(_REAL), owner_of=_owner(_REAL),
    )
    assert got is not None and got.parcel_id != "6411600002"
