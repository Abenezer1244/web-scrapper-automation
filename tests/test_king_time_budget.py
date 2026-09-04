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


# ── Phase ordering (Codex P1 on the chunking refactor) ───────────────────────
# batch_enrich_king_county is phase 1 (cheap HTTP: property + OWNER) then phase 2
# (Playwright mailing, ~10-20x costlier). A caller that chunks its parcel list and
# calls this per chunk inverts their priority: chunk 1's phase 2 spends the whole
# shared budget and later chunks never get phase 1 at all. In prod that took a
# 17,157-parcel job from ~1,200 parcels reached down to 173. The driver now runs
# phase 1 across every parcel first (do_mailing=False, collecting tax_urls_out)
# and drives phase 2 afterwards (tax_urls_in) with the budget left over.

def test_phase1_only_pass_skips_mailing_and_exports_tax_urls():
    from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county

    stats: dict = {}
    urls: dict = {}
    # Zero budget: no network. What matters is that the phase-1-only contract
    # exists and never enters the Playwright phase.
    out = asyncio.run(batch_enrich_king_county(
        ["1234567890"], time_budget_s=0, stats=stats,
        do_mailing=False, tax_urls_out=urls,
    ))
    assert out == {}
    assert urls == {}
    # Phase 2 never ran, so it never reported mailing attempts.
    assert stats["mailing_attempted"] == 0
    assert stats["mailing_found"] == 0


def test_mailing_only_pass_does_not_refetch_phase_one():
    # tax_urls_in drives phase 2 alone. With no budget it must defer rather than
    # silently report "no mailing address" for parcels it never looked at.
    from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county

    stats: dict = {}
    out = asyncio.run(batch_enrich_king_county(
        [], time_budget_s=0, stats=stats,
        tax_urls_in={"1234567890": "https://payment.kingcounty.gov/x"},
    ))
    assert out == {}
    assert stats["requested"] == 1
    assert stats["budget_exhausted"] is True
    assert "1234567890" in stats["deferred"]


def test_default_call_still_runs_both_phases_in_order():
    # Defaults must preserve the original single-call behaviour exactly, so the
    # backfill scripts and any other caller are unaffected.
    from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county

    stats: dict = {}
    asyncio.run(batch_enrich_king_county(["1234567890"], time_budget_s=0, stats=stats))
    assert stats["budget_exhausted"] is True
    assert stats["deferred"] == ["1234567890"]
