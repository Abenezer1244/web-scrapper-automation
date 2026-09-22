"""NTS matcher scoring tests — the false-match firewall.

Wrong auction data on a lead is worse than missing it, so these pin: parcel/address
agreement thresholds, the never-match-on-address-or-name-alone rule, and the
ambiguity skip. No DB — pure scoring.
"""
from src.scrapers.sources.nts_matcher import (
    MATCH_THRESHOLD,
    PARCEL_BRIDGED,
    PARCEL_CONFLICT,
    PARCEL_EXACT,
    PARCEL_UNKNOWN,
    best_match,
    best_match_group,
    parcel_index_keys,
    parcel_relation,
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

    def test_conflicting_parcels_block_match(self):
        # different parcels = different property; do NOT fall through to address
        # (Codex: two units at same street+zip with same surname must not match)
        assert _score(np_="P1", nk=KEY_A, ng="X SMITH", rp="P2", rk=KEY_A, rn="SMITH") == 0.0

    def test_one_parcel_missing_uses_address(self):
        # parcel only on one side (no conflict) -> address+grantor path still works
        assert _score(nk=KEY_A, ng="X SMITH", rp="P2", rk=KEY_A, rn="SMITH JOHN") == 0.92

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

    def test_conflicting_parcel_unit_collision_not_matched(self):
        # notice + two same-street units; the unit whose parcel CONFLICTS is blocked,
        # only the parcel-agreeing unit can match
        notice = self._notice(parcel="P1", key=KEY_A, grantor="SMITH")
        cands = [
            {"id": "right", "parcel": "P1", "addr_key": KEY_A, "party_name": "SMITH JOHN"},
            {"id": "wrong_unit", "parcel": "P2", "addr_key": KEY_A, "party_name": "SMITH JANE"},
        ]
        m = best_match(notice, cands)
        assert m is not None and m[0] == "right"  # wrong_unit scored 0.0, not ambiguous

    def test_non_string_parcel_does_not_crash(self):
        assert score_match(
            notice_parcel=12345, notice_addr_key=None, notice_grantor=None,
            result_parcel=12345, result_addr_key=None, result_party_name=None,
        ) == 0.90  # int parcels coerced, exact-match


# ── King PIN/account bridge ────────────────────────────────────────────────────
#
# King publishes one property under two identifiers: the recorder index emits the
# 10-digit PIN (2895650150) while the trustee prints the 12-digit tax ACCOUNT number
# (289565-0150-04) on the Notice of Trustee Sale. Before the bridge those read as a
# parcel CONFLICT and scored 0.0 — a hard veto nothing could override — which vetoed
# 7 real notice/lead pairs in prod (measured 2026-09-19), two of them live auctions.
#
# The bridge is deliberately narrow: county-gated, digits only, and ASYMMETRIC 10-vs-12
# with a prefix relation. It demands BOTH corroborators before it may auto-attach,
# because it is an inference about the county's encoding and not an observed identity.

# The one prod pair where BOTH corroborators survive (TS WA07000188-22-3, auction
# 2026-09-25, $190,752.06). The recorder row happens to carry the ZIP, so the two
# address keys agree.
KING_PIN = "4216400220"
KING_ACCOUNT = "421640022008"
KING_KEY = "11120 NE 68TH ST|98033"
KING_NOTICE_GRANTOR = "JOANN SIMON, AN UNMARRIED INDIVIDUAL"
KING_PARTY = "SIMON JOANN"


def _king(np_=None, nk=None, ng=None, rp=None, rk=None, rn=None, county="king") -> float:
    return score_match(
        notice_parcel=np_, notice_addr_key=nk, notice_grantor=ng,
        result_parcel=rp, result_addr_key=rk, result_party_name=rn, county=county,
    )


class TestParcelRelation:
    def test_equal_is_exact(self):
        assert parcel_relation("051928-5029", "0519285029", county="king") == PARCEL_EXACT

    def test_pin_then_account_is_bridged(self):
        assert parcel_relation(KING_PIN, KING_ACCOUNT, county="king") == PARCEL_BRIDGED

    def test_account_then_pin_is_bridged_both_orders(self):
        assert parcel_relation(KING_ACCOUNT, KING_PIN, county="king") == PARCEL_BRIDGED

    def test_hyphenated_account_normalizes_then_bridges(self):
        assert parcel_relation("289565-0150-04", "2895650150", county="king") == PARCEL_BRIDGED

    def test_two_12_digit_accounts_sharing_a_pin_do_not_bridge(self):
        # Different account numbers on the same PIN are NOT provably one property, and
        # bridging them would make the relation transitive (Codex P1).
        assert parcel_relation("289565015004", "289565015099", county="king") == PARCEL_CONFLICT

    def test_10_vs_12_without_prefix_does_not_bridge(self):
        assert parcel_relation("1234567890", "999999999999", county="king") == PARCEL_CONFLICT

    def test_only_10_vs_12_lengths_bridge(self):
        # 9/12, 10/11, 11/12, 12/13 are all conflicts even with a prefix relation.
        for a, b in (
            ("123456789", "123456789012"),
            ("1234567890", "12345678901"),
            ("12345678901", "123456789012"),
            ("123456789012", "1234567890123"),
        ):
            assert parcel_relation(a, b, county="king") == PARCEL_CONFLICT, (a, b)

    def test_non_digit_parcel_never_bridges(self):
        # 10 and 12 chars with a prefix relation, but alphanumeric -> not King's scheme.
        assert parcel_relation("A234567890", "A23456789012", county="king") == PARCEL_CONFLICT

    def test_other_counties_with_the_same_shape_do_not_bridge(self):
        for county in ("pierce", "snohomish", "clark", "spokane"):
            assert parcel_relation(KING_PIN, KING_ACCOUNT, county=county) == PARCEL_CONFLICT

    def test_missing_county_fails_closed(self):
        assert parcel_relation(KING_PIN, KING_ACCOUNT) == PARCEL_CONFLICT
        assert parcel_relation(KING_PIN, KING_ACCOUNT, county=None) == PARCEL_CONFLICT
        assert parcel_relation(KING_PIN, KING_ACCOUNT, county="") == PARCEL_CONFLICT

    def test_county_is_case_and_whitespace_safe(self):
        for county in ("King", "KING", "  king  ", "\tKing\n"):
            assert parcel_relation(KING_PIN, KING_ACCOUNT, county=county) == PARCEL_BRIDGED

    def test_missing_parcel_is_unknown_not_conflict(self):
        for a, b in ((None, KING_PIN), (KING_PIN, None), (None, None), ("", KING_PIN),
                     ("   ", KING_PIN), (0, KING_PIN)):
            assert parcel_relation(a, b, county="king") == PARCEL_UNKNOWN, (a, b)

    def test_malformed_values_do_not_crash(self):
        for a in (12345, 3.5, [], {}, object()):
            assert parcel_relation(a, KING_PIN, county="king") in (
                PARCEL_EXACT, PARCEL_BRIDGED, PARCEL_CONFLICT, PARCEL_UNKNOWN,
            )


class TestBridgeScoring:
    def test_bridge_with_address_and_grantor_reaches_threshold(self):
        s = _king(np_=KING_ACCOUNT, nk=KING_KEY, ng=KING_NOTICE_GRANTOR,
                  rp=KING_PIN, rk=KING_KEY, rn=KING_PARTY)
        assert s == 0.95 and s >= MATCH_THRESHOLD

    def test_bridge_scores_below_every_corroborated_exact_match(self):
        # A true exact parcel must always beat a bridged one for the same lead.
        exact = _score(np_="P1", nk=KEY_A, ng="SMITH", rp="P1", rk=KEY_A, rn="SMITH JOHN")
        assert 0.95 < exact == 0.99

    def test_bridge_with_address_only_is_rejected(self):
        assert _king(np_=KING_ACCOUNT, nk=KING_KEY, ng="SOMEONE ELSE",
                     rp=KING_PIN, rk=KING_KEY, rn="NOBODY MATCHING") == 0.0

    def test_bridge_with_grantor_only_is_rejected(self):
        assert _king(np_=KING_ACCOUNT, nk=KING_KEY, ng=KING_NOTICE_GRANTOR,
                     rp=KING_PIN, rk=KEY_B, rn=KING_PARTY) == 0.0

    def test_bridge_with_no_corroborator_never_inherits_the_bare_090(self):
        # The bare-exact branch returns exactly MATCH_THRESHOLD; a bridge must not
        # reach it on the encoding inference alone (Codex P1).
        assert _king(np_=KING_ACCOUNT, rp=KING_PIN) == 0.0

    def test_bridge_with_missing_address_on_one_side_is_rejected(self):
        # King recorder rows often store a street-only property_address (the frozen
        # dedup key), so the notice key carries a ZIP the lead key does not. That is
        # NOT address agreement and must not auto-attach under the chosen rule.
        assert _king(np_="289565-0150-04", nk="4206 S GREENBELT STATION DR|98118",
                     ng="KAMAL KISHORE KORKONDA AND SWAPNA CHODIMELLA, A MARRIED COUPLE",
                     rp="2895650150", rk="4206 S GREENBELT STATION DR",
                     rn="KORKONDA KAMAL KISHORE / CHODIMELLA SWAPNA") == 0.0

    def test_real_conflict_stays_a_hard_veto_even_with_address_and_grantor(self):
        assert _king(np_="1111111111", nk=KEY_A, ng="SMITH JOHN",
                     rp="2222222222", rk=KEY_A, rn="JOHN SMITH") == 0.0

    def test_exact_parcels_are_unchanged_by_the_county_argument(self):
        for county in (None, "king", "pierce"):
            assert _king(np_="P1", rp="P1", county=county) == 0.90
            assert _king(np_="P1", nk=KEY_A, rp="P1", rk=KEY_A, county=county) == 0.97

    def test_address_only_path_unchanged_by_the_county_argument(self):
        assert _king(nk=KEY_A, ng="JOHN SMITH", rk=KEY_A, rn="SMITH JOHN") == 0.92
        assert _king(nk=KEY_A, rk=KEY_A) == 0.80


class TestBridgeGrouping:
    def _notice(self, parcel=None, key=None, grantor=None):
        return {"parcel": parcel, "property_address_normalized": key, "grantor": grantor}

    def _cand(self, cid, parcel, key=KING_KEY, name=KING_PARTY):
        return {"id": cid, "parcel": parcel, "addr_key": key, "party_name": name}

    def test_pin_and_account_rows_group_together(self):
        # Two tenants hold the same King property, one row spelled with the PIN and one
        # with the account number. Both must receive the notice.
        notice = self._notice(parcel=KING_ACCOUNT, key=KING_KEY, grantor=KING_NOTICE_GRANTOR)
        group = best_match_group(
            notice,
            [self._cand("pin", KING_PIN), self._cand("acct", KING_ACCOUNT)],
            county="king",
        )
        assert {cid for cid, _ in group} == {"pin", "acct"}

    def test_mixed_forms_do_not_bail_the_group_to_empty(self):
        # Regression for Codex P1: patching only score_match left _same_property
        # comparing normalized strings, so a bridged sibling read as a DIFFERENT
        # property and returned [] — strictly worse than the veto it replaced.
        notice = self._notice(parcel=KING_PIN, key=KING_KEY, grantor=KING_NOTICE_GRANTOR)
        group = best_match_group(
            notice,
            [self._cand("acct", KING_ACCOUNT), self._cand("pin", KING_PIN)],
            county="king",
        )
        assert len(group) == 2

    def test_two_distinct_accounts_on_one_pin_fail_closed(self):
        # Non-transitivity guard: the PIN bridges to BOTH accounts, but the accounts
        # conflict with each other, so which one sorts first must not decide it.
        notice = self._notice(parcel=KING_PIN, key=KING_KEY, grantor=KING_NOTICE_GRANTOR)
        group = best_match_group(
            notice,
            [self._cand("a", "421640022008"), self._cand("b", "421640022099")],
            county="king",
        )
        assert group == []

    def test_group_outcome_is_independent_of_candidate_order(self):
        notice = self._notice(parcel=KING_PIN, key=KING_KEY, grantor=KING_NOTICE_GRANTOR)
        a, b = self._cand("a", "421640022008"), self._cand("b", "421640022099")
        assert best_match_group(notice, [a, b], county="king") == \
               best_match_group(notice, [b, a], county="king") == []

    def test_a_genuinely_different_property_still_bails(self):
        notice = self._notice(parcel=KING_ACCOUNT, key=KING_KEY, grantor=KING_NOTICE_GRANTOR)
        group = best_match_group(
            notice,
            [self._cand("right", KING_PIN),
             {"id": "other", "parcel": "9999999999", "addr_key": KING_KEY,
              "party_name": KING_PARTY}],
            county="king",
        )
        # 'other' conflicts on parcel so it scores 0.0 and never reaches the group.
        assert {cid for cid, _ in group} == {"right"}

    def test_non_king_county_does_not_group_mixed_forms(self):
        notice = self._notice(parcel=KING_ACCOUNT, key=KING_KEY, grantor=KING_NOTICE_GRANTOR)
        assert best_match_group(
            notice, [self._cand("pin", KING_PIN)], county="pierce"
        ) == []


class TestParcelIndexKeys:
    def test_pin_indexes_under_itself_only(self):
        assert parcel_index_keys(KING_PIN, "king") == {KING_PIN}

    def test_account_also_indexes_under_its_pin(self):
        assert parcel_index_keys("289565-0150-04", "king") == {"289565015004", "2895650150"}

    def test_non_bridging_county_indexes_under_itself_only(self):
        assert parcel_index_keys(KING_ACCOUNT, "pierce") == {KING_ACCOUNT}

    def test_missing_parcel_yields_no_keys(self):
        for v in (None, "", "   ", 0):
            assert parcel_index_keys(v, "king") == set()

    def test_pool_lookup_finds_a_bridged_candidate(self):
        # The pool is keyed on these; a notice carrying the ACCOUNT must reach a lead
        # carrying the PIN, and vice versa.
        assert parcel_index_keys(KING_ACCOUNT, "king") & parcel_index_keys(KING_PIN, "king")
