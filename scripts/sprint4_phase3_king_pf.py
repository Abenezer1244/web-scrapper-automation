"""Sprint 4 Phase 3: King pre-foreclosure email check via enriched addresses.

King stores pre-foreclosure property_address as 'STREET ZIP' (no city).
Enrich each record via WA statewide GIS to get city before submitting
to Tracerfy advanced trace. Tests whether living owners (mortgage
defaulters) have measurable email coverage vs deceased probate.

Small sample (5 records → 10 credits) to conserve budget.
"""
import os
import re
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
from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis
from src.scrapers.enrichment.skip_trace import (
    TracerfyError,
    classify_grantor_as_entity,
    split_name,
    submit_batch,
)


def main() -> int:
    engine = create_engine(settings.DATABASE_URL.replace("+asyncpg", ""))

    print("=== Sprint 4 Phase 3: King pre-foreclosure email check ===\n")

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT ON (r.property_address)
                   r.id, r.parcel_id, r.party_name, r.property_address
            FROM results r
            JOIN jobs j ON r.job_id = j.id
            JOIN scraper_configs sc ON j.scraper_config_id = sc.id
            WHERE sc.county = 'king'
              AND sc.record_type = 'pre_foreclosure'
              AND r.parcel_id IS NOT NULL
              AND r.property_address IS NOT NULL
            ORDER BY r.property_address, r.created_at DESC
            LIMIT 5
        """)).fetchall()

    print(f"Pulled {len(rows)} unique King pre-foreclosure records")
    if len(rows) < 3:
        print("Not enough. Aborting.")
        return 1

    # Enrich city via WA statewide GIS (free)
    parcel_ids = [r.parcel_id for r in rows]
    print(f"\nEnriching {len(parcel_ids)} parcels via WA statewide GIS...")
    gis_results = batch_enrich_parcels_gis(parcel_ids, "king", "WA")
    print(f"  GIS returned {len(gis_results)} enriched records")

    # Build Tracerfy payload
    payload_rows = []
    for row in rows:
        gis_data = gis_results.get(row.parcel_id, {})
        gis_mailing = gis_data.get("mailing_address") or ""
        gis_prop = gis_data.get("property_address") or row.property_address

        # Parse "STREET, CITY, WA 98101" format from GIS
        # WA statewide returns: "11185 SE BEAN RD, PORT ORCHARD, WA 98366"
        parts = [p.strip() for p in gis_mailing.split(",") if p.strip()] if gis_mailing else []
        street = parts[0] if parts else gis_prop
        city = parts[1] if len(parts) >= 2 else ""
        zipcode = ""
        if len(parts) >= 3:
            # Last part: "WA 98366"
            m = re.search(r"\b(\d{5}(?:-\d{4})?)\b", parts[2])
            if m:
                zipcode = m.group(1)
        # Also try to pull ZIP from the original property_address (has "98115" suffix)
        if not zipcode:
            m2 = re.search(r"\b(\d{5})\b", row.property_address or "")
            if m2:
                zipcode = m2.group(1)

        first_name, last_name = split_name(row.party_name)
        is_entity = classify_grantor_as_entity(row.party_name)

        payload_rows.append({
            "address": street,
            "city": city,
            "state": "WA",
            "zip": zipcode,
            "first_name": "" if is_entity else (first_name or ""),
            "last_name": "" if is_entity else (last_name or ""),
            "mail_address": "",
            "mail_city": "",
            "mail_state": "",
            "mailing_zip": "",
        })

    print("\nEnriched payload:")
    for i, p in enumerate(payload_rows):
        print(f"  {i+1}. {p['address'][:35]!r} city={p['city']!r} zip={p['zip']!r} last={p['last_name']!r}")

    # Skip records with no city (Tracerfy will reject)
    valid_rows = [p for p in payload_rows if p["city"]]
    dropped = len(payload_rows) - len(valid_rows)
    if dropped:
        print(f"\n  Dropping {dropped} rows with no city (GIS miss)")
    if not valid_rows:
        print("No rows with city. Aborting.")
        return 1

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_before = r.json().get("balance", 0)
    print(f"\nbalance before: {balance_before}")

    print(f"\nSubmitting {len(valid_rows)} rows, trace_type=advanced")
    try:
        response = submit_batch(valid_rows, trace_type="advanced")
    except TracerfyError as exc:
        print(f"SUBMIT FAILED: {exc}")
        return 1

    queue_id = response["queue_id"]
    rows_uploaded = response.get("rows_uploaded")
    print(f"SUBMITTED. queue_id={queue_id}  rows_uploaded={rows_uploaded}")

    with engine.connect() as conn:
        test_uid = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar()
        conn.execute(text("""
            INSERT INTO skip_trace_queues
                (id, tracerfy_queue_id, job_id, user_id, trace_type, status,
                 rows_uploaded, credits_deducted, submitted_at)
            VALUES
                (:id, :qid, NULL, :uid, 'advanced', 'pending', :rows, 0, :now)
        """), {
            "id": str(uuid.uuid4()),
            "qid": queue_id,
            "uid": str(test_uid),
            "rows": rows_uploaded,
            "now": datetime.now(UTC),
        })
        conn.commit()

    start = time.time()
    timeout = 120
    completed = False
    print("\nPolling...")
    while time.time() - start < timeout:
        time.sleep(10)
        elapsed = int(time.time() - start)
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
            print(f"  [{elapsed}s] DB: {str(exc)[:60]}")
        print(f"  [{elapsed}s] pending...")

    if not completed:
        print("TIMEOUT")
        return 1

    print()
    print("=== King pre-foreclosure (living owners) hit rate ===")
    with engine.connect() as conn:
        cache_rows = conn.execute(text("""
            SELECT phone, phone_type, email
            FROM skip_trace_cache
            WHERE fetched_at >= NOW() - INTERVAL '2 minutes'
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
