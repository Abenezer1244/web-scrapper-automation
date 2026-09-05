"""Pin the King enrichment two-pass contract in the DEPLOYED worker path.

The canary (scripts/canary_king_phase1_throughput.py) drives
`batch_enrich_king_county` directly, so it proves the helper's phase-1-only mode
works and measures King's real latency — but it would still PASS if the worker
that actually runs in production had drifted back to calling the helper once per
chunk with both phases (Codex P1). That drift is exactly the bug #215 fixed: the
expensive Playwright mailing phase consuming the whole budget inside chunk 1, so a
17,157-parcel job reached 173 parcels instead of ~1,200.

These tests read the production caller and assert the ordering contract holds:
phase 1 runs across every parcel first (`do_mailing=False`, collecting
`tax_urls_out`), and only then does mailing run (`tax_urls_in`).
"""
import re
from pathlib import Path

_ENRICH = Path(__file__).resolve().parents[1] / "src" / "workers" / "tasks_helpers" / "enrich.py"


def _king_block() -> str:
    """The King WA branch of the inline enrichment pass."""
    src = _ENRICH.read_text(encoding="utf-8")
    start = src.index('config.county.lower() == "king"')
    # Ends where the post-enrichment (non-King) section begins.
    end = src.index("Post-enrichment", start)
    return src[start:end]


def test_phase_one_runs_with_mailing_disabled():
    block = _king_block()
    assert "do_mailing=False" in block, (
        "the King driver no longer runs a phase-1-only pass; if it calls "
        "batch_enrich_king_county with mailing enabled per chunk, chunk 1's "
        "Playwright phase will consume the whole budget again"
    )
    assert "tax_urls_out=" in block, (
        "phase 1 must collect tax-bill URLs for the later mailing pass"
    )


def test_mailing_pass_is_driven_separately_and_comes_second():
    block = _king_block()
    assert "tax_urls_in=" in block, "no separate mailing pass found"
    # Ordering is the whole point: the phase-1-only call must appear BEFORE the
    # mailing-only call in the driver.
    assert block.index("do_mailing=False") < block.index("tax_urls_in="), (
        "the mailing pass runs before phase 1 has covered every parcel — this is "
        "the exact inversion that reached 173 of 17,157 parcels"
    )


def test_mailing_pass_is_seeded_with_phase_one_rows():
    # Phase 2 validates the rendered tax page against resolved_parcel_id, so a
    # recovered parcel silently loses its mailing address if the pass starts from
    # an empty results dict.
    block = _king_block()
    assert "results_seed=" in block, (
        "the mailing pass is not seeded with phase 1's rows; recovered parcels "
        "would fail page validation and drop their mailing address"
    )


def test_both_passes_commit_per_chunk():
    # Progress must survive a worker restart; deploys routinely kill in-flight jobs.
    block = _king_block()
    assert "_run_chunk" in block, "the chunked/committing driver is gone"
    assert re.search(r"while\s+_pending", block), "phase 1 no longer loops over chunks"
    assert re.search(r"while\s+_mail_pending", block), "phase 2 no longer loops over chunks"
