"""NTS matcher scoring tests — the false-match firewall.

Wrong auction data on a lead is worse than missing it, so these pin: parcel/address
agreement thresholds, the never-match-on-address-or-name-alone rule, and the
ambiguity skip. No DB — pure scoring.
"""
from src.scrapers.sources.nts_matcher import (
    MATCH_THRESHOLD,
    best_match,
    score_match,
)

# A consistent address normalized key for a property (as address_match_key produces).
KEY_A = "123 MAIN ST|98401"
KEY_B = "999 OTHER AVE|98444"


def _score(np_=None, nk=None, ng=None, rp=None, rk=None, rn=None) -> float:
    return score_match(
        notice_parcel=np_, notice_addr_key=nk, notice_grantor=ng,
        result_parcel=rp, result_addr_key=rk, result_party_name=rn,
    )


class TestScore:
    def test_parcel_exact_alone_meets_threshold(self):
        assert _score(np_="051928-5029", rp="0519285029") == 0.90  # hyphen-normalized

    def test_parcel_plus_address(self):
        assert _score(np_="P1", nk=KEY_A, rp="P1", rk=KEY_A) == 0.97

    def test_parcel_plus_address_plus_grantor(self):
        assert _score(np_="P1", nk=KEY_A, ng="SPURLING MARK", rp="P1", rk=KEY_A, rn="MARK SPURLING") == 0.99

    def test_address_plus_grantor_meets_threshold(self):
        assert _score(nk=KEY_A, ng="JOHN SMITH", rk=KEY_A, rn="SMITH JOHN AND JANE") == 0.92

    def test_address_alone_below_threshold(self):
        s = _score(nk=KEY_A, rk=KEY_A)
        assert s == 0.80 and s < MATCH_THRESHOLD  # NOT auto-matched

    def test_grantor_only_zero(self):
        assert _score(ng="JOHN SMITH", rn="SMITH JOHN") == 0.0

    def test_nothing_zero(self):
        assert _score() == 0.0

    def test_parcel_mismatch_falls_to_address(self):
        # different parcels but same address+grantor -> address path (0.92), not parcel
        assert _score(np_="P1", nk=KEY_A, ng="X SMITH", rp="P2", rk=KEY_A, rn="SMITH") == 0.92

    def test_grantor_token_set_order_independent(self):
        assert _score(np_="P1", ng="JANE AND JOHN SMITH", rp="P1", rn="SMITH JOHN") == 0.96


class TestBestMatch:
    def _notice(self, parcel=None, key=None, grantor=None):
        return {"parcel": parcel, "property_address_normalized": key, "grantor": grantor}

    def test_single_strong_match(self):
        notice = self._notice(parcel="P1", key=KEY_A, grantor="SMITH")
        cands = [
            {"id": "a", "parcel": "P1", "addr_key": KEY_A, "party_name": "SMITH JOHN"},
            {"id": "b", "parcel": "PX", "addr_key": KEY_B, "party_name": "DOE JANE"},
        ]
        m = best_match(notice, cands)
        assert m is not None and m[0] == "a" and m[1] >= MATCH_THRESHOLD

    def test_no_candidate_reaches_threshold(self):
        notice = self._notice(key=KEY_A)  # address-only
        cands = [{"id": "a", "parcel": "PX", "addr_key": KEY_A, "party_name": "X"}]
        assert best_match(notice, cands) is None  # 0.80 < threshold

    def test_ambiguous_two_above_threshold_skipped(self):
        # two leads share the parcel (e.g. duplicate Results) -> ambiguous -> skip
        notice = self._notice(parcel="P1", key=KEY_A, grantor="SMITH")
        cands = [
            {"id": "a", "parcel": "P1", "addr_key": KEY_A, "party_name": "SMITH JOHN"},
            {"id": "b", "parcel": "P1", "addr_key": KEY_A, "party_name": "SMITH JANE"},
        ]
        assert best_match(notice, cands) is None

    def test_one_strong_one_weak_picks_strong(self):
        notice = self._notice(parcel="P1", key=KEY_A, grantor="SMITH")
        cands = [
            {"id": "strong", "parcel": "P1", "addr_key": KEY_A, "party_name": "SMITH"},   # 0.99
            {"id": "weak", "parcel": "PX", "addr_key": KEY_A, "party_name": "OTHER"},     # 0.80
        ]
        m = best_match(notice, cands)
        assert m is not None and m[0] == "strong"

    def test_empty_candidates(self):
        assert best_match(self._notice(parcel="P1"), []) is None
