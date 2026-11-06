"""No customer-visible or public number counts a privacy-withheld row (Phase 6 P3b-2).

The counts the read inventory (docs/legal/audit-2026-11/07-RESULTS-READ-INVENTORY.md)
found outside the central predicate: the results page's enriched count and auction
coverage, the batch list's contacts-looked-up rollup, and the public landing-page
numbers. Real database and endpoints; synthetic rows; the flag is set as the enforcer
will set it (tests/test_privacy_read_paths._withhold).
"""
import uuid
from datetime import date

from httpx import AsyncClient
from sqlalchemy import text

from src.db.models import Result, ScraperConfig, User
from src.workers.scheduler_helpers.public_cache import _refresh_public_sample_cache_impl
from tests.test_batch_run_summary import _list, _make_batch
from tests.test_privacy_read_paths import _fixture, _job, _results, _withhold


async def test_the_enriched_count_skips_withheld_rows(
    client: AsyncClient, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig, db,
):
    job, _, _ = await _fixture(db, starter_user, scraper_config)
    # Non-duplicates with a property address: NEW and HELD shown, XNEW and XHELD withheld.
    assert (await _results(client, job, starter_token))["enriched_count"] == 2


async def test_auction_coverage_skips_withheld_rows(
    client: AsyncClient, starter_user: User, starter_token: str, db,
):
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Pre-foreclosure",
        county="pierce", state="WA", record_type="pre_foreclosure", fields=[],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"emails": []})
    db.add(cfg)
    await db.commit()
    job = await _job(db, starter_user, cfg)
    ids = []
    for tag in ("A", "B"):
        rid = str(uuid.uuid4())
        db.add(Result(id=rid, job_id=job, user_id=starter_user.id, party_name=f"OWNER {tag}",
                      property_address=f"{tag} FIR LN", dedup_hash=uuid.uuid4().hex,
                      is_duplicate=False, enrichment_data={}, auction_date=date(2026, 12, 1)))
        ids.append(rid)
    await db.commit()
    await _withhold(db, ids[1])

    body = await _results(client, job, starter_token)
    assert body["auction_coverage"]["matched"] == 1


async def test_the_batch_rollup_does_not_count_a_withheld_rows_lookup(
    client: AsyncClient, db, starter_user: User, starter_token: str,
):
    _, [job] = await _make_batch(db, starter_user.id, "privacy-rollup", [
        ("pierce", "probate", "done", 2, True, [
            {"skip_trace_status": "hit", "skip_trace_source": "lookup"},
            {"skip_trace_status": "hit", "skip_trace_source": "lookup"},
        ]),
    ])
    [withheld] = (await db.execute(text(
        "SELECT id::text FROM results WHERE job_id = :j ORDER BY party_name LIMIT 1"),
        {"j": job})).scalars().all()
    await _withhold(db, withheld)
    assert (await _list(client, starter_token))["privacy-rollup"]["contacts_looked_up"] == 1


async def test_the_public_numbers_never_count_a_withheld_row(
    starter_user: User, db,
):
    """A county that only a withheld row would add must not appear in counties_active;
    the same row unflagged does (positive control)."""
    county = f"zz-privacy-{uuid.uuid4().hex[:8]}"
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=starter_user.id, name="Only county", county=county,
        state="WA", record_type="probate", fields=[], enrichment=[],
        schedule={"frequency": "manual"}, deliver={"emails": []})
    db.add(cfg)
    await db.commit()
    job = await _job(db, starter_user, cfg)
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, job_id=job, user_id=starter_user.id, party_name="OWNER Z",
                  property_address="1 ZED ST", dedup_hash=uuid.uuid4().hex,
                  is_duplicate=False, enrichment_data={}))
    await db.commit()
    await _withhold(db, rid)

    async def counties() -> int:
        _refresh_public_sample_cache_impl()
        return (await db.execute(text(
            "SELECT (payload->>'counties_active')::int FROM public_sample_cache WHERE id = 1"
        ))).scalar()

    withheld = await counties()
    await db.execute(text("UPDATE results SET privacy_suppressed_at = NULL, "
                          "privacy_suppression_id = NULL WHERE id = :i"), {"i": rid})
    await db.commit()
    assert await counties() == withheld + 1

