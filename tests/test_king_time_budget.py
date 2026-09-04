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


# ── Owner-only pass: partial results must survive (Test 10 regression) ────────
# The inline owner pass used to be capped at 25 parcels and wrapped in a
# wait_for() that CANCELLED the coroutine on timeout. Cancellation discarded every
# owner name resolved up to that point, so a job that ran long finished with zero
# owner names rather than "as many as it got to" — the same lose-everything shape
# the mailing budget above was written to fix. The caller now owns the result dict
# and the loop stops cooperatively on its own budget.

def test_owner_lookup_writes_into_caller_dict_so_partials_survive():
    from src.scrapers.enrichment.king_county_assessor import batch_extract_king_owners

    owners: dict[str, str] = {}
    returned = asyncio.run(batch_extract_king_owners(
        ["1234567890", "2345678901"], out=owners, time_budget_s=0,
    ))
    # Same object: whatever had been resolved is already in the caller's hands
    # even if the coroutine is cancelled before it can return.
    assert returned is owners


def test_owner_lookup_zero_budget_stops_before_any_network_call():
    from src.scrapers.enrichment.king_county_assessor import batch_extract_king_owners

    owners: dict[str, str] = {}
    asyncio.run(batch_extract_king_owners(
        ["1234567890", "2345678901"], out=owners, time_budget_s=0,
    ))
    assert owners == {}


def test_owner_lookup_without_out_still_returns_a_dict():
    # Backwards compatibility for the backfill script's existing call shape.
    from src.scrapers.enrichment.king_county_assessor import batch_extract_king_owners

    assert asyncio.run(batch_extract_king_owners([], time_budget_s=5)) == {}
