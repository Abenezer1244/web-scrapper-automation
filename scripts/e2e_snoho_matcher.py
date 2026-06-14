"""E2E (Postgres-only) verification: the NTS matcher attaches Result.auction_date
onto real Snohomish pre_foreclosure leads.

Why not run_scrape_job: that task publishes progress to redis.railway.internal, which
is unreachable when executed locally via `railway run`. This script exercises the SAME
real pieces that matter for the verification — the registry-resolved scraper, real
nts_notices cache, real Result persistence, and the REAL match_job_inline (the exact
function the scrape pipeline calls at tasks.py:896) — over Postgres only.

Flow:
  1. Refresh the Snohomish nts_notices cache (real crawl_nts_snoho_tribune).
  2. Resolve + run the connector's scraper -> Snohomish NTS ScrapedRecords.
  3. Reuse (or create) a Snohomish pre_foreclosure Job, persist the leads as Result rows
     (auction_date NULL), exactly the columns the pipeline writes.
  4. Run match_job_inline(db, job_id) -> the matcher attaches auction_date county-scoped.
  5. Report how many leads got auction_date + sample, then finalize the job status.

Run:
    railway run --service worker python scripts/e2e_snoho_matcher.py
"""
import asyncio
import sys
import uuid

sys.path.insert(0, ".")


async def _scrape():
    from src.scrapers.snohomish_wa_pre_foreclosure import SnohomishWAPreForeclosureScraper

    async with SnohomishWAPreForeclosureScraper(record_type="pre_foreclosure") as s:
        return await s.scrape("2000-01-01", "2100-01-01")


def main() -> int:
    from sqlalchemy import select, text

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import SyncSessionLocal
    from src.scrapers.registry import get_scraper_class
    from src.workers.nts_crawler import crawl_nts_snoho_tribune
    from src.workers.nts_matcher_task import match_job_inline

    print("=== Step 1: refresh Snohomish nts_notices cache ===")
    print(f"  crawl: {crawl_nts_snoho_tribune()}")

    print("\n=== Step 2: resolve connector + scrape ===")
    cls, rt = get_scraper_class("snohomish", "wa", "pre_foreclosure")
    print(f"  registry -> {getattr(cls, '__name__', cls)} ({rt})")
    records = asyncio.run(_scrape())
    print(f"  scraped {len(records)} Snohomish NTS lead(s)")
    if not records:
        print("  no leads scraped — cannot verify. Aborting.")
        return 2

    with SyncSessionLocal() as db:
        n_notices = db.execute(
            text(
                "SELECT count(*) FROM nts_notices WHERE lower(county)='snohomish' "
                "AND is_active AND auction_date >= CURRENT_DATE"
            )
        ).scalar()
        print(f"  active future-dated snohomish notices in cache: {n_notices}")

        # Reuse a Snohomish pre_foreclosure job with 0 results (the orphan from the
        # eager-run attempt); else create a fresh config+job under an admin user.
        owner_id = db.execute(
            text("SELECT id FROM users WHERE is_admin = true ORDER BY created_at LIMIT 1")
        ).scalar() or db.execute(text("SELECT id FROM users ORDER BY created_at LIMIT 1")).scalar()

        job_id = db.execute(
            text(
                """
                SELECT j.id FROM jobs j
                JOIN scraper_configs sc ON sc.id = j.scraper_config_id
                WHERE lower(sc.county)='snohomish' AND sc.record_type='pre_foreclosure'
                  AND NOT EXISTS (SELECT 1 FROM results r WHERE r.job_id = j.id)
                ORDER BY j.created_at DESC LIMIT 1
                """
            )
        ).scalar()

        if job_id:
            print(f"\n=== Step 3: reuse orphan job {job_id} ===")
            job_user = db.execute(text("SELECT user_id FROM jobs WHERE id=:j"), {"j": job_id}).scalar()
        else:
            print("\n=== Step 3: create fresh config + job ===")
            tmpl = db.execute(
                select(ScraperConfig).where(ScraperConfig.record_type == "pre_foreclosure").limit(1)
            ).scalar_one_or_none()
            cfg = ScraperConfig(
                id=str(uuid.uuid4()), user_id=owner_id,
                name="E2E Snohomish pre_foreclosure (matcher verify)",
                county="snohomish", state="wa", record_type="pre_foreclosure",
                fields=(tmpl.fields if tmpl else []), enrichment=(tmpl.enrichment if tmpl else []),
                schedule={}, deliver={}, skip_trace_enabled=False, doc_types=None,
            )
            db.add(cfg)
            job = Job(id=str(uuid.uuid4()), user_id=owner_id,
                      scraper_config_id=cfg.id, status="running", trigger="manual")
            db.add(job)
            db.flush()
            job_id, job_user = job.id, owner_id
        print(f"  job={job_id} user={job_user}")

        # Persist the scraped leads as Result rows (the pipeline's writer columns).
        inserted = 0
        for rec in records:
            db.add(Result(
                id=str(uuid.uuid4()), job_id=job_id, user_id=job_user,
                party_name=rec.party_name, property_address=rec.property_address,
                parcel_id=rec.parcel_id, doc_type=rec.doc_type,
                date_recorded=rec.date_recorded, enrichment_data=rec.enrichment_data,
            ))
            inserted += 1
        db.commit()
        print(f"  persisted {inserted} Result row(s) (auction_date NULL)")

        print("\n=== Step 4: run match_job_inline (the real pipeline matcher) ===")
        matched = match_job_inline(db, job_id)
        print(f"  match_job_inline wrote auction data to {matched} lead(s)")

        print("\n=== Step 5: verify ===")
        total = db.execute(text("SELECT count(*) FROM results WHERE job_id=:j"), {"j": job_id}).scalar()
        with_auction = db.execute(
            text("SELECT count(*) FROM results WHERE job_id=:j AND auction_date IS NOT NULL"),
            {"j": job_id},
        ).scalar()
        rows = db.execute(
            text(
                "SELECT party_name, property_address, parcel_id, auction_date, "
                "default_amount, nts_match_confidence FROM results WHERE job_id=:j "
                "ORDER BY auction_date NULLS LAST"
            ),
            {"j": job_id},
        ).fetchall()
        # Finalize the job so it isn't left dangling.
        db.execute(text("UPDATE jobs SET status='done' WHERE id=:j"), {"j": job_id})
        db.commit()

    print(f"  leads: {total}   with auction_date attached: {with_auction}")
    for r in rows:
        print(
            f"   - {str(r.party_name)[:32]!r} {str(r.property_address)[:40]!r} "
            f"parcel={r.parcel_id} auction={r.auction_date} default={r.default_amount} "
            f"conf={r.nts_match_confidence}"
        )
    ok = bool(total) and with_auction == total
    print(
        f"\nRESULT: {'PASS' if ok else 'PARTIAL/FAIL'} — matcher attached auction_date to "
        f"{with_auction}/{total} Snohomish lead(s)"
    )
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
