"""Sprint 4 Phase 3: advanced trace comparison.

Runs advanced trace (2 credits/row = 20 credits for 10 rows) on the
same Pierce probate records to see if email coverage improves vs
normal trace's 0%.
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
    submit_batch,
)


def main() -> int:
    engine = create_engine(settings.DATABASE_URL.replace("+asyncpg", ""))

    print("=== Sprint 4 Phase 3: advanced trace comparison ===\n")

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT r.id, r.party_name, r.property_address, r.mailing_address
            FROM results r
            JOIN jobs j ON r.job_id = j.id
            JOIN scraper_configs sc ON j.scraper_config_id = sc.id
            WHERE sc.county = 'pierce'
              AND sc.record_type = 'probate'
              AND r.property_address IS NOT NULL
              AND r.mailing_address IS NOT NULL
              AND r.mailing_address LIKE '%, WA%'
            ORDER BY r.created_at DESC
            LIMIT 10
        """)).fetchall()

    print(f"Pulled {len(rows)} records")

    payload_rows = []
    for row in rows:
        mail_parsed = _parse_full_address(row.mailing_address or "")
        payload_rows.append({
            "address": mail_parsed["street"] or row.property_address or "",
            "city": mail_parsed["city"] or "",
            "state": mail_parsed["state"] or "WA",
            "zip": mail_parsed["zip"] or "",
            "first_name": "",
            "last_name": "",
            "mail_address": "",
            "mail_city": "",
            "mail_state": "",
            "mailing_zip": "",
        })

    # Balance check
    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_before = r.json().get("balance", 0)
    print(f"balance before: {balance_before}")

    # Submit advanced
    trace_type = "advanced"
    expected_cost = len(payload_rows) * 2
    print(f"\nSubmitting {len(payload_rows)} rows, trace_type={trace_type} (expected {expected_cost} credits)")

    try:
        response = submit_batch(payload_rows, trace_type=trace_type)
    except TracerfyError as exc:
        print(f"SUBMIT FAILED: {exc}")
        return 1

    queue_id = response["queue_id"]
    print(f"SUBMITTED. queue_id={queue_id}  rows_uploaded={response.get('rows_uploaded')}")

    # Insert DB row FIRST this time, before polling
    with engine.connect() as conn:
        test_user_id = conn.execute(text("SELECT id FROM users WHERE is_admin = true LIMIT 1")).scalar()
        if not test_user_id:
            test_user_id = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar()
        conn.execute(
            text("""
                INSERT INTO skip_trace_queues
                    (id, tracerfy_queue_id, job_id, user_id, trace_type, status,
                     rows_uploaded, credits_deducted, submitted_at)
                VALUES
                    (:id, :qid, NULL, :uid, 'advanced', 'pending', :rows, 0, :now)
            """),
            {
                "id": str(uuid.uuid4()),
                "qid": queue_id,
                "uid": str(test_user_id),
                "rows": len(payload_rows),
                "now": datetime.now(UTC),
            },
        )
        conn.commit()

    # Wait for webhook
    start = time.time()
    timeout = 300
    completed = False
    print(f"\nPolling for webhook (up to {timeout}s)...")
    while time.time() - start < timeout:
        time.sleep(15)
        elapsed = int(time.time() - start)
        with engine.connect() as conn:
            st = conn.execute(
                text("SELECT status, credits_deducted FROM skip_trace_queues WHERE tracerfy_queue_id = :qid"),
                {"qid": queue_id},
            ).fetchone()
            if st and st.status == "completed":
                print(f"  [{elapsed}s] COMPLETED. credits={st.credits_deducted}")
                completed = True
                break
        r = requests.get(
            "https://tracerfy.com/v1/api/analytics/",
            headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
            timeout=15,
        )
        data = r.json()
        print(f"  [{elapsed}s] tracerfy balance={data.get('balance')}")

    if not completed:
        print(f"TIMEOUT after {timeout}s")
        return 1

    # Measure hit rate from cache rows added for THIS queue.
    # The ingest code writes by address_hash upsert, so rows from the
    # normal-trace run earlier may be updated rather than duplicated.
    # Count cache rows from the last 5 minutes.
    print("\n=== Measuring advanced hit rate ===")
    with engine.connect() as conn:
        cache_rows = conn.execute(text("""
            SELECT phone, phone_type, email
            FROM skip_trace_cache
            WHERE fetched_at >= NOW() - INTERVAL '5 minutes'
            ORDER BY fetched_at DESC
            LIMIT 20
        """)).fetchall()

        n = len(cache_rows)
        phones = sum(1 for r in cache_rows if r.phone)
        emails = sum(1 for r in cache_rows if r.email)
        print(f"  Recent cache rows: {n}")
        if n > 0:
            phone_pct = 100 * phones / n
            email_pct = 100 * emails / n
            print(f"  Phone: {phones}/{n} ({phone_pct:.0f}%)")
            print(f"  Email: {emails}/{n} ({email_pct:.0f}%)")
            print()
            print("  Normal vs Advanced comparison:")
            print("    Normal:   phone 7/10 (70%)  email 0/10 (0%)")
            print(f"    Advanced: phone {phones}/{n} ({phone_pct:.0f}%)  email {emails}/{n} ({email_pct:.0f}%)")

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_after = r.json().get("balance", 0)
    print(f"\nBalance after: {balance_after} (spent {balance_before - balance_after} this run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
