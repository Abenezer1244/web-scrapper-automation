"""Phase 5 — dialer-ready eligibility predicate (Enzo-independent foundation).

Pure unit tests (no DB). The DB application (predicates ANDed into get_results /
download) is exercised in CI; here we lock the TCPA-safe default (known not-DNC
only) vs the opt-in candidate set (unknown-DNC allowed), and the predicate shape.
"""
from src.api.dialer_filters import dialer_ready_conditions


def _sql(conds) -> str:
    return " ".join(str(c.compile(compile_kwargs={"literal_binds": False})) for c in conds)


class TestDialerReady:
    def test_default_is_tcpa_safe(self):
        # Default (ready): valid phone + phone_dnc_flag IS FALSE — unknown DNC
        # is EXCLUDED. 3 predicates: not-null, non-empty, dnc IS FALSE.
        conds = dialer_ready_conditions()
        assert len(conds) == 3
        sql = _sql(conds).lower()
        assert "phone is not null" in sql
        assert "trim(results.phone)" in sql
        # Known-false DNC only (IS false), not "IS NOT true".
        assert "phone_dnc_flag is false" in sql

    def test_candidate_includes_unknown_dnc(self):
        # Opt-in candidate set: exclude only KNOWN-DNC (IS NOT TRUE allows NULL).
        conds = dialer_ready_conditions(include_unknown_dnc=True)
        assert len(conds) == 3
        sql = _sql(conds).lower()
        assert "phone_dnc_flag is not true" in sql

    def test_ready_and_candidate_differ_only_on_dnc(self):
        ready = _sql(dialer_ready_conditions()).lower()
        cand = _sql(dialer_ready_conditions(include_unknown_dnc=True)).lower()
        # Both require a valid phone...
        for s in (ready, cand):
            assert "phone is not null" in s
            assert "trim(results.phone)" in s
        # ...and differ on DNC strictness.
        assert ready != cand

    def test_no_skip_trace_gate(self):
        # Provenance-agnostic: a valid phone qualifies regardless of how it was
        # obtained — skip_trace_status must NOT be baked into "ready".
        sql = _sql(dialer_ready_conditions()).lower()
        assert "skip_trace_status" not in sql
