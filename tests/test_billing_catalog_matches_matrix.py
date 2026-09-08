"""The customer-facing plan catalog must not promise access the matrix denies."""
from src.api.routes.billing import _PLANS
from src.config.constants import COUNTY_LIMIT_BY_PLAN, RECORD_TYPES_BY_PLAN


def _plan(pid: str) -> dict:
    return next(p for p in _PLANS if p["id"] == pid)


def test_pro_does_not_claim_all_record_types():
    feats = " ".join(_plan("pro")["features"]).lower()
    # Pro is the 4 core lists (incl. Auction Leads), NOT all types.
    assert "all record types" not in feats
    assert len(RECORD_TYPES_BY_PLAN["pro"]) == 4


def test_pro_advertises_its_three_county_cap():
    feats = " ".join(_plan("pro")["features"]).lower()
    assert "3 counties" in feats
    assert COUNTY_LIMIT_BY_PLAN["pro"] == 3


def test_business_advertises_all_types_and_ten_counties():
    feats = " ".join(_plan("business")["features"]).lower()
    assert "all record types" in feats
    assert "10 counties" in feats
    assert COUNTY_LIMIT_BY_PLAN["business"] == 10
    assert RECORD_TYPES_BY_PLAN["business"] == RECORD_TYPES_BY_PLAN["agency"]


# ── the comparison table, not just the plan bullets ──────────────────────────
# The bullets above were already guarded. The comparison dict served in the SAME
# /billing/pricing response was not, which is how it came to say Pro got "All"
# record types while the bullets beside it correctly named four and the API
# answered 402 for the rest. Every cell that describes a gate is now derived from
# that gate; these tests are the proof, and the reason nobody has to trust that.

import pytest

from src.api.routes.billing import pricing_page
from src.config.constants import (
    ALL_RECORD_TYPES,
    BATCH_PLANS,
    OVERLAP_PLANS,
    PRIORITY_QUEUE_PLANS,
    allowed_export_formats,
    allowed_schedule_frequencies,
)
from src.config.settings import settings

_PLAN_IDS = ("starter", "pro", "business", "agency")


@pytest.mark.asyncio
async def test_the_comparison_record_types_row_agrees_with_the_gate():
    comparison = (await pricing_page())["comparison"]["Record types"]
    assert comparison["starter"] == "Probate"
    assert comparison["pro"] == "Pre-Foreclosure, Probate, Tax Delinquent, Trustee Sale"
    for plan in ("business", "agency"):
        assert comparison[plan] == "All"
        assert RECORD_TYPES_BY_PLAN[plan] == ALL_RECORD_TYPES


@pytest.mark.asyncio
async def test_the_comparison_export_and_schedule_rows_agree_with_the_gates():
    comparison = (await pricing_page())["comparison"]
    assert comparison["Export formats"]["starter"] == "CSV"
    assert comparison["Export formats"]["pro"] == "CSV, Excel"
    assert comparison["Export formats"]["business"] == "All formats"
    assert comparison["Scheduling"]["starter"] == "Manual only"
    assert comparison["Scheduling"]["pro"] == "Daily, Weekly"
    assert comparison["Scheduling"]["business"] == "All frequencies"
    # And the cells are derived, not retyped: a change to the matrix moves them.
    assert "json" not in allowed_export_formats("pro")
    assert "monthly" not in allowed_schedule_frequencies("pro")


@pytest.mark.asyncio
async def test_the_comparison_skip_trace_row_carries_the_included_amount():
    """Pro read "Per-lookup", which dropped the 250 its own bullet includes."""
    comparison = (await pricing_page())["comparison"]["Skip tracing"]
    assert comparison["starter"] is False
    for plan in ("pro", "business", "agency"):
        assert comparison[plan] == f"{settings.SKIP_TRACE_BUNDLED_QUOTAS[plan]:,} included"


@pytest.mark.asyncio
async def test_the_comparison_feature_rows_agree_with_their_allowlists():
    comparison = (await pricing_page())["comparison"]
    for plan in _PLAN_IDS:
        assert comparison["Overlap and intersection lists"][plan] == (plan in OVERLAP_PLANS)
        assert comparison["Batch scraping"][plan] == (plan in BATCH_PLANS)
        assert comparison["Priority queue"][plan] == (plan in PRIORITY_QUEUE_PLANS)


@pytest.mark.asyncio
async def test_the_comparison_claims_nothing_the_product_does_not_have():
    """Team seats sat here as 1 / 1 / 5 / Unlimited with no seat model anywhere:
    no invite flow, no member table, no route. White-label is not built and has
    to keep its "coming soon" qualifier wherever it appears."""
    comparison = (await pricing_page())["comparison"]
    assert "Team members" not in comparison
    assert comparison["White-label"]["agency"] == "Coming soon"
    assert comparison["White-label"]["business"] is False


@pytest.mark.asyncio
async def test_email_delivery_is_not_advertised_as_a_gate_nothing_enforces():
    """The row said Starter False. `deliver.emails` is accepted on every plan and
    the Starter card never claimed otherwise, so the row was the thing that was
    wrong."""
    comparison = (await pricing_page())["comparison"]["Email delivery"]
    assert all(comparison[plan] is True for plan in _PLAN_IDS)
