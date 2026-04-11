"""Sprint 4 Phase 3: full round-trip verification against real Pierce probate data.

Burns ~10 credits. Does NOT modify existing Result rows - only writes to
skip_trace_cache and skip_trace_queues. Webhook ingest will upsert cache
rows when Tracerfy webhooks our production API.

This is a production-grade integration test (not a mock/dummy). It:
  1. Pulls 10 real Pierce probate Result rows with addresses from prod DB
  2. Classifies each grantor entity vs individual (same logic the dispatcher uses)
  3. Submits a single batch via Tracerfy /v1/api/trace/
  4. Inserts a SkipTraceQueue row so webhook ingest can correlate
  5. Polls for webhook completion + measures phone/email hit rate
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

    print("=== Sprint 4 Phase 3: full round-trip verification ===\n")

    # 1) Pull real Pierce probate Result rows. Pierce stores city+state+ZIP
    # in mailing_address (format: "STREET, CITY, WA, ZIP"), not in
    # property_address. We parse mailing_address to get the full address
    # components Tracerfy needs.
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT r.id, r.party_name, r.property_address, r.mailing_address
            FROM results r
            JOIN jobs j ON r.job_id = j.id
            JOIN scraper_configs sc ON j.scraper_config_id = sc.id
            WHERE sc.county = 'pierce'
              AND sc.record_type = 'probate'
              AND r.property_address IS NOT NULL
              AND r.property_address != ''
              AND r.property_address != '(enrichment unavailable)'
              AND r.mailing_address IS NOT NULL
              AND r.mailing_address LIKE '%, WA%'
            ORDER BY r.created_at DESC
            LIMIT 10
        """)).fetchall()

    print(f"Pulled {len(rows)} Pierce probate records with addresses")

    if len(rows) < 5:
        print("Not enough records for verification. Aborting.")
        return 1

    # 2) Build payload like the dispatcher. For this test we parse
    # mailing_address (which has the full "STREET, CITY, STATE ZIP" for
    # Pierce county GIS enriched records) to get city + state + zip.
    # In production the dispatcher's build_pending_row_payload handles
    # this via _parse_full_address on property_address — which works for
    # Clark/King/Whatcom where property_address includes city, but NOT
    # for Pierce. That's a follow-up to fix in Phase 4 (or earlier if
    # the Pierce scraper can be updated to store a combined address).
    payload_rows = []
    entity_count = 0
    for row in rows:
        # Parse mailing_address for the full components
        mail_parsed = _parse_full_address(row.mailing_address or "")
        street = mail_parsed["street"] or row.property_address or ""
        city = mail_parsed["city"] or ""
        state = mail_parsed["state"] or "WA"
        zipcode = mail_parsed["zip"] or ""

        first_name, last_name = split_name(row.party_name)
        is_entity = classify_grantor_as_entity(row.party_name)
        if is_entity:
            entity_count += 1

        payload_rows.append({
            "address": street,
            "city": city,
            "state": state,
            "zip": zipcode,
            "first_name": "" if is_entity else (first_name or ""),
            "last_name": "" if is_entity else (last_name or ""),
            "mail_address": "",
            "mail_city": "",
            "mail_state": "",
            "mailing_zip": "",
        })

    print(f"\nClassification:")
    print(f"  entities (grantor is LLC/Trust/etc): {entity_count}")
    print(f"  individuals: {len(payload_rows) - entity_count}")

    print(f"\nSample payload rows:")
    for i, pr in enumerate(payload_rows[:3]):
        print(f"  {i+1}. addr={pr['address'][:40]!r} city={pr['city']!r} state={pr['state']!r}")
        print(f"     first={pr['first_name']!r} last={pr['last_name']!r}")

    # Pre-flight balance check
    print(f"\nPre-flight: Tracerfy balance check...")
    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_before = r.json().get("balance", 0)
    print(f"  balance before: {balance_before} credits")

    if balance_before < len(payload_rows):
        print(f"Insufficient credits ({balance_before} < {len(payload_rows)}). Aborting.")
        return 1

    # 3) Submit batch as NORMAL trace (1 credit/row). Pierce mailing_address
    # gives us full city/state/zip so the cheap mode works.
    trace_type = "normal"
    credit_multiplier = 2 if trace_type == "advanced" else 1
    expected_cost = len(payload_rows) * credit_multiplier
    print(f"\nSubmitting batch of {len(payload_rows)} rows, trace_type={trace_type} ...")
    print(f"Expected cost: {expected_cost} credits (~${expected_cost * 0.02:.2f})")

    try:
        response = submit_batch(payload_rows, trace_type=trace_type)
    except TracerfyError as exc:
        print(f"SUBMIT FAILED: {exc}")
        return 1

    queue_id = response["queue_id"]
    estimated_wait = response.get("estimated_wait_seconds", 60)
    print(f"\nSUBMITTED.")
    print(f"  queue_id={queue_id}")
    print(f"  rows_uploaded={response.get('rows_uploaded')}")
    print(f"  estimated_wait={estimated_wait}s")

    # 4) Insert SkipTraceQueue row for webhook correlation
    with engine.connect() as conn:
        test_user_id = conn.execute(
            text("SELECT id FROM users WHERE is_admin = true LIMIT 1")
        ).scalar()
        if not test_user_id:
            test_user_id = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar()
        print(f"  Using user_id={test_user_id[:8]}... for SkipTraceQueue row")

        conn.execute(
            text("""
                INSERT INTO skip_trace_queues
                    (id, tracerfy_queue_id, job_id, user_id, trace_type, status,
                     rows_uploaded, credits_deducted, submitted_at)
                VALUES
                    (:id, :tracerfy_queue_id, NULL, :user_id, :trace_type, 'pending',
                     :rows, 0, :now)
            """),
            {
                "id": str(uuid.uuid4()),
                "tracerfy_queue_id": queue_id,
                "user_id": test_user_id,
                "trace_type": trace_type,
                "rows": len(payload_rows),
                "now": datetime.now(UTC),
            },
        )
        conn.commit()
    print(f"  Inserted skip_trace_queues row")

    # 5) Poll for completion
    print(f"\nPolling for webhook completion (up to {max(estimated_wait + 180, 300)}s)...")
    start = time.time()
    timeout = max(estimated_wait + 180, 300)
    completed = False
    while time.time() - start < timeout:
        time.sleep(15)
        elapsed = int(time.time() - start)

        with engine.connect() as conn:
            st = conn.execute(
                text("""
                    SELECT status, rows_uploaded, credits_deducted, completed_at
                    FROM skip_trace_queues
                    WHERE tracerfy_queue_id = :qid
                """),
                {"qid": queue_id},
            ).fetchone()
            if st and st.status == "completed":
                print(f"  [{elapsed}s] WEBHOOK RECEIVED. rows={st.rows_uploaded} credits={st.credits_deducted}")
                completed = True
                break

        r = requests.get(
            "https://tracerfy.com/v1/api/analytics/",
            headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
            timeout=15,
        )
        data = r.json()
        print(f"  [{elapsed}s] tracerfy: pending={data.get('queues_pending')} completed={data.get('queues_completed')} balance={data.get('balance')}")

    if not completed:
        print(f"\nTIMEOUT after {timeout}s. Webhook did not arrive.")
        print(f"Debug: SELECT * FROM skip_trace_queues WHERE tracerfy_queue_id = {queue_id}")
        return 1

    # 6) Measure hit rate from cache (what the webhook just wrote)
    print(f"\n=== Measuring hit rate ===")
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with engine.connect() as conn:
        cache_rows = conn.execute(text("""
            SELECT phone, phone_type, email
            FROM skip_trace_cache
            WHERE fetched_at >= :since
            ORDER BY fetched_at DESC
            LIMIT :lim
        """), {"since": today_start, "lim": len(payload_rows)}).fetchall()

        n = len(cache_rows)
        phones = sum(1 for r in cache_rows if r.phone)
        emails = sum(1 for r in cache_rows if r.email)
        print(f"  Cache rows written today: {n}")
        if n > 0:
            phone_pct = 100 * phones / n
            email_pct = 100 * emails / n
            print(f"  Phone hits: {phones}/{n} ({phone_pct:.0f}%)")
            print(f"  Email hits: {emails}/{n} ({email_pct:.0f}%)")
            print()
            print(f"  PRD gate: phone >= 60%, email >= 25%")
            print(f"  Phone:  {'PASS' if phone_pct >= 60 else 'FAIL'}")
            print(f"  Email:  {'PASS' if email_pct >= 25 else 'FAIL'}")

    # Final balance
    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_after = r.json().get("balance", 0)
    print(f"\nBalance after: {balance_after} (spent {balance_before - balance_after} credits)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
