"""King County mailing lookup honours an internal time budget and returns PARTIAL
results instead of being cancelled from outside (2026-09-02: every large King tax
job died in the caller's wait_for(240) and lost skip-trace enqueue).

No network: a zero budget is exhausted before the first HTTP fetch. Needs the test
DB only for the source-health gate the function consults first.
"""
import asyncio

from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county


def test_zero_budget_defers_every_parcel_without_network():
    stats: dict = {}
    out = asyncio.run(batch_enrich_king_county(["1234567890", "2345678901"], time_budget_s=0, stats=stats))
    assert out == {}
    assert stats["budget_exhausted"] is True
    assert stats["deferred"] == ["1234567890", "2345678901"]
    assert stats["requested"] == 2 and stats["mailing_attempted"] == 0 and stats["mailing_found"] == 0


def test_stats_default_shape_when_nothing_to_do():
    stats: dict = {}
    assert asyncio.run(batch_enrich_king_county([], time_budget_s=5, stats=stats)) == {}
    assert stats["requested"] == 0 and stats["deferred"] == [] and stats["budget_exhausted"] is False
