"""Sprint 4 Phase 3: King pre-foreclosure email coverage check.

Tests whether living owners (pre-foreclosure defaulters) have any email
coverage vs deceased probate records which showed 0%.
"""
import os
import sys
import time
import uuid
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from dotenv import load_dotenv

load_dotenv(".env")

import requests
from sqlalchemy import create_engine, text

from src.config import settings
from src.scrapers.enrichment.skip_trace import (
    TracerfyError,
    _parse_full_address,
    classify_grantor_as_entity,
    split_name,
    submit_batch,
)


def main() -> int:
    engine = create_engine(settings.DATABASE_URL.replace("+asyncpg", ""))

    print("=== Sprint 4 Phase 3: pre-foreclosure email check ===\n")

    with engine.connect() as conn:
        # King pre-foreclosure with full mailing addresses
        rows = conn.execute(text("""
            SELECT DISTINCT ON (r.property_address)
                   r.id, r.party_name, r.property_address, r.mailing_address
            FROM results r
            JOIN jobs j ON r.job_id = j.id
            JOIN scraper_configs sc ON j.scraper_config_id = sc.id
            WHERE sc.county = 'king'
              AND sc.record_type = 'pre_foreclosure'
              AND r.property_address IS NOT NULL
              AND r.mailing_address IS NOT NULL
              AND r.mailing_address LIKE '%, WA%'
              AND r.party_name IS NOT NULL
            ORDER BY r.property_address, r.created_at DESC
            LIMIT 10
        """)).fetchall()

    if not rows:
        # Fallback: Pierce pre-foreclosure
        print("No King pre-foreclosure records with mailing — trying Pierce...")
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT ON (r.property_address)
                       r.id, r.party_name, r.property_address, r.mailing_address
                FROM results r
                JOIN jobs j ON r.job_id = j.id
                JOIN scraper_configs sc ON j.scraper_config_id = sc.id
                WHERE sc.county = 'pierce'
                  AND sc.record_type = 'pre_foreclosure'
                  AND r.property_address IS NOT NULL
                  AND r.mailing_address IS NOT NULL
                  AND r.mailing_address LIKE '%, WA%'
                ORDER BY r.property_address, r.created_at DESC
                LIMIT 10
            """)).fetchall()

    print(f"Pulled {len(rows)} UNIQUE pre-foreclosure records")

    if len(rows) < 5:
        print("Not enough pre-foreclosure records. Aborting.")
        return 1

    payload_rows = []
    for row in rows:
        mail_parsed = _parse_full_address(row.mailing_address or "")
        first_name, last_name = split_name(row.party_name)
        is_entity = classify_grantor_as_entity(row.party_name)
        payload_rows.append({
            "address": mail_parsed["street"] or row.property_address or "",
            "city": mail_parsed["city"] or "",
            "state": mail_parsed["state"] or "WA",
            "zip": mail_parsed["zip"] or "",
            "first_name": "" if is_entity else (first_name or ""),
            "last_name": "" if is_entity else (last_name or ""),
            "mail_address": "",
            "mail_city": "",
            "mail_state": "",
            "mailing_zip": "",
        })

    print("\nSample payload:")
    for i, p in enumerate(payload_rows[:3]):
        print(f"  {i+1}. {p['address'][:40]!r} {p['city']!r} {p['state']} {p['zip']}")

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_before = r.json().get("balance", 0)
    print(f"\nbalance before: {balance_before}")

    trace_type = "advanced"
    print(f"\nSubmitting {len(payload_rows)} rows, trace_type={trace_type}")
    try:
        response = submit_batch(payload_rows, trace_type=trace_type)
    except TracerfyError as exc:
        print(f"SUBMIT FAILED: {exc}")
        return 1

    queue_id = response["queue_id"]
    rows_uploaded = response.get("rows_uploaded")
    print(f"SUBMITTED. queue_id={queue_id}  rows_uploaded={rows_uploaded}")

    with engine.connect() as conn:
        test_user_id = conn.execute(text("SELECT id FROM users WHERE is_admin = true LIMIT 1")).scalar() \
                    or conn.execute(text("SELECT id FROM users LIMIT 1")).scalar()
        conn.execute(
            text("""
                INSERT INTO skip_trace_queues
                    (id, tracerfy_queue_id, job_id, user_id, trace_type, status,
                     rows_uploaded, credits_deducted, submitted_at)
                VALUES
                    (:id, :qid, NULL, :uid, :tt, 'pending', :rows, 0, :now)
            """),
            {
                "id": str(uuid.uuid4()),
                "qid": queue_id,
                "uid": str(test_user_id),
                "tt": trace_type,
                "rows": rows_uploaded,
                "now": datetime.now(UTC),
            },
        )
        conn.commit()

    start = time.time()
    timeout = 180
    completed = False
    print("\nPolling...")
    while time.time() - start < timeout:
        time.sleep(15)
        elapsed = int(time.time() - start)
        # Use a fresh connection each iteration to avoid idle timeouts
        try:
            with engine.connect() as conn:
                st = conn.execute(
                    text("SELECT status, credits_deducted FROM skip_trace_queues WHERE tracerfy_queue_id = :qid"),
                    {"qid": queue_id},
                ).fetchone()
                if st and st.status == "completed":
                    print(f"  [{elapsed}s] COMPLETED. credits={st.credits_deducted}")
                    completed = True
                    break
        except Exception as exc:
            print(f"  [{elapsed}s] DB hiccup: {str(exc)[:60]}")
        print(f"  [{elapsed}s] still pending...")

    if not completed:
        print("TIMEOUT — check Railway logs")
        return 1

    print()
    print("=== Pre-foreclosure hit rate ===")
    with engine.connect() as conn:
        cache_rows = conn.execute(text("""
            SELECT phone, phone_type, email
            FROM skip_trace_cache
            WHERE fetched_at >= NOW() - INTERVAL '3 minutes'
            ORDER BY fetched_at DESC
        """)).fetchall()
        n = len(cache_rows)
        phones = sum(1 for r in cache_rows if r.phone)
        emails = sum(1 for r in cache_rows if r.email)
        print(f"  Recent cache: {n}")
        if n:
            print(f"  Phone: {phones}/{n} ({100*phones/n:.0f}%)")
            print(f"  Email: {emails}/{n} ({100*emails/n:.0f}%)")

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_after = r.json().get("balance", 0)
    print(f"\nbalance after: {balance_after} (spent {balance_before - balance_after})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
