"""Sprint 4 Phase 3: run production scrape jobs for Thurston/Kitsap/Whatcom.

End-to-end test of the full Sprint 4 pipeline:
  scrape -> GIS enrich -> drop unactionable -> skip-trace enqueue
  -> dispatcher submit -> Tracerfy -> webhook -> DB cache

Steps:
  1. Fix Whatcom connector: switch to dedicated WhatcomWAScraper
  2. Pick an Agency user with skip_trace headroom
  3. Create scraper_configs for each of the 3 counties (probate)
     with skip_trace_enabled=True
  4. Create Job rows and enqueue run_scrape_job via Celery
  5. Poll each job to completion
  6. Measure: records scraped, enrichment rate, skip-trace queued
  7. Wait for dispatcher + webhook round-trip
  8. Final per-county phone/email hit rate
"""
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from dotenv import load_dotenv
load_dotenv(".env")

import requests
from sqlalchemy import create_engine, text

from src.config import settings

COUNTIES = ["thurston", "kitsap", "whatcom"]


def main() -> int:
    engine = create_engine(settings.DATABASE_URL.replace("+asyncpg", ""), echo=False)

    print("=== Sprint 4 Phase 3: 3-county production prod scrape ===\n")

    # 1) Fix Whatcom connector
    with engine.connect() as conn:
        res = conn.execute(
            text("""
                UPDATE county_connectors
                SET scraper_mode = 'manual',
                    scraper_class = 'src.scrapers.whatcom_wa.WhatcomWAScraper'
                WHERE LOWER(county) = 'whatcom' AND active = true
                  AND (scraper_mode = 'ai' OR scraper_class = '' OR scraper_class IS NULL)
            """)
        )
        conn.commit()
        print(f"Whatcom connector updated: {res.rowcount} rows")

    # 2) Find an Agency user
    with engine.connect() as conn:
        user = conn.execute(text("""
            SELECT id, email, plan
            FROM users
            WHERE plan IN ('agency', 'business', 'pro')
              AND email NOT LIKE 'e2e_%'  -- skip throwaway test accounts
            ORDER BY
              CASE plan WHEN 'agency' THEN 1 WHEN 'business' THEN 2 ELSE 3 END,
              created_at ASC
            LIMIT 1
        """)).fetchone()
        if not user:
            print("No usable user found. Aborting.")
            return 1
        user_id = str(user.id)
        print(f"Using user: {user.email} (plan={user.plan}) id={user_id[:8]}")

    # 3) Ensure scraper_configs exist for each county (probate, skip_trace_enabled=true)
    config_ids = {}
    for county in COUNTIES:
        with engine.connect() as conn:
            existing = conn.execute(text("""
                SELECT id FROM scraper_configs
                WHERE user_id = :uid
                  AND LOWER(county) = :county
                  AND record_type = 'probate'
                  AND active = true
                LIMIT 1
            """), {"uid": user_id, "county": county}).scalar()

            if existing:
                # Update it to enable skip_trace_enabled
                conn.execute(text("""
                    UPDATE scraper_configs
                    SET skip_trace_enabled = true
                    WHERE id = :id
                """), {"id": existing})
                conn.commit()
                config_ids[county] = str(existing)
                print(f"[{county}] using existing scraper_config {str(existing)[:8]} (skip_trace_enabled=true)")
            else:
                new_id = str(uuid.uuid4())
                conn.execute(text("""
                    INSERT INTO scraper_configs
                        (id, user_id, name, county, state, record_type,
                         fields, enrichment, schedule, deliver,
                         skip_trace_enabled, active)
                    VALUES
                        (:id, :uid, :name, :county, 'WA', 'probate',
                         '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                         true, true)
                """), {
                    "id": new_id,
                    "uid": user_id,
                    "name": f"Sprint4 Verify - {county.title()} probate",
                    "county": county,
                })
                conn.commit()
                config_ids[county] = new_id
                print(f"[{county}] created scraper_config {new_id[:8]}")

    # 4) Create Job rows and enqueue via Celery
    from src.workers.tasks import run_scrape_job

    job_ids = {}
    for county, config_id in config_ids.items():
        job_id = str(uuid.uuid4())
        with engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO jobs
                    (id, user_id, scraper_config_id, status, trigger,
                     page_current, page_total, record_count, retry_count)
                VALUES
                    (:id, :uid, :cfg, 'pending', 'manual', 0, 0, 0, 0)
            """), {
                "id": job_id,
                "uid": user_id,
                "cfg": config_id,
            })
            conn.commit()
        job_ids[county] = job_id

        run_scrape_job.delay(job_id)
        print(f"[{county}] enqueued job {job_id[:8]} via Celery")

    # 5) Poll job completion
    print()
    print("Polling job completion (up to 15 min)...")
    start = time.time()
    timeout = 900
    pending_jobs = set(job_ids.keys())
    while pending_jobs and time.time() - start < timeout:
        time.sleep(20)
        elapsed = int(time.time() - start)
        with engine.connect() as conn:
            status_rows = conn.execute(text("""
                SELECT id, status, record_count FROM jobs WHERE id IN :ids
            """).bindparams(ids=tuple(job_ids.values()))).fetchall()
        statuses = {}
        for r in status_rows:
            for county, jid in job_ids.items():
                if str(r.id) == jid:
                    statuses[county] = (r.status, r.record_count)
        line = "  [" + str(elapsed) + "s]"
        for county in COUNTIES:
            s, n = statuses.get(county, ("?", 0))
            line += f"  {county}={s}:{n}"
        print(line)
        pending_jobs = {c for c, (s, _) in statuses.items() if s in ("pending", "queued", "probing", "scraping", "enriching")}

    if pending_jobs:
        print(f"TIMEOUT after {timeout}s. Still pending: {pending_jobs}")

    # 6) Measure per-county results
    print()
    print("=== Scrape + enrich summary per county ===")
    for county, job_id in job_ids.items():
        with engine.connect() as conn:
            stats = conn.execute(text("""
                SELECT COUNT(*) as total,
                       COUNT(property_address) as with_prop,
                       COUNT(phone) as with_phone,
                       COUNT(email) as with_email,
                       COUNT(CASE WHEN skip_trace_status = 'queued' THEN 1 END) as queued,
                       COUNT(CASE WHEN skip_trace_status = 'hit' THEN 1 END) as cache_hits,
                       COUNT(CASE WHEN skip_trace_status = 'not_attempted' THEN 1 END) as not_attempted
                FROM results
                WHERE job_id = :jid
            """), {"jid": job_id}).fetchone()
            print(f"[{county}] total={stats.total}  prop={stats.with_prop}  phone={stats.with_phone}  email={stats.with_email}  "
                  f"queued={stats.queued}  cache_hits={stats.cache_hits}  not_attempted={stats.not_attempted}")

    # 7) Wait for dispatcher + Tracerfy + webhook
    print()
    print("Waiting 8 min for dispatcher -> Tracerfy -> webhook cycle...")
    time.sleep(60 * 8)

    # 8) Final per-county hit rate
    print()
    print("=== Final per-county skip-trace hit rate ===")
    for county, job_id in job_ids.items():
        with engine.connect() as conn:
            stats = conn.execute(text("""
                SELECT COUNT(*) as total,
                       COUNT(phone) as with_phone,
                       COUNT(email) as with_email
                FROM results
                WHERE job_id = :jid
                  AND property_address IS NOT NULL
            """), {"jid": job_id}).fetchone()
            n = stats.total
            if n:
                p = stats.with_phone
                e = stats.with_email
                p_pct = 100 * p / n
                e_pct = 100 * e / n
                verdict = "PASS" if p_pct >= 60 and e_pct >= 25 else "PARTIAL"
                print(f"[{county}] {n} records  phone={p}/{n} ({p_pct:.0f}%)  email={e}/{n} ({e_pct:.0f}%)  {verdict}")
            else:
                print(f"[{county}] 0 records")

    # Balance check
    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    print(f"\nTracerfy balance: {r.json().get('balance')} credits remaining")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
