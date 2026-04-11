"""Sprint 4 multi-county verification: advanced trace on all 7 Sprint-2 counties.

For each county, pulls up to 10 unique probate records with a parseable
address (city + state + zip from mailing_address, OR enriches via WA
statewide GIS if the scraper stored address without city).

Submits one advanced-trace batch per county, waits for all webhooks,
then prints a per-county scorecard with phone + email hit rates.

Cost: up to 2 credits × 10 records × 7 counties = 140 credits (~$2.80).
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
    _parse_full_address,
    classify_grantor_as_entity,
    split_name,
    submit_batch,
)

COUNTIES = ["pierce", "king", "clark", "spokane", "thurston", "kitsap", "whatcom"]


def extract_city_state_zip(mailing_address: str, property_address: str, county: str) -> tuple[str, str, str, str]:
    """Return (street, city, state, zip). Tries mailing_address first
    (has 4-part comma format for Pierce/others), then WA statewide GIS
    for counties that store address without city.

    Uses inline extraction to avoid extra DB round trips — caller passes
    what they've already got.
    """
    # First try parsing mailing_address
    if mailing_address:
        parsed = _parse_full_address(mailing_address)
        if parsed["city"] and parsed["state"]:
            return (
                parsed["street"] or property_address or "",
                parsed["city"],
                parsed["state"],
                parsed["zip"] or "",
            )

    # Second try parsing property_address (Clark/Whatcom have city in there)
    if property_address:
        parsed = _parse_full_address(property_address)
        if parsed["city"] and parsed["state"]:
            return (
                parsed["street"] or property_address,
                parsed["city"],
                parsed["state"],
                parsed["zip"] or "",
            )

    # No city recoverable
    return (property_address or "", "", "WA", "")


def main() -> int:
    engine = create_engine(settings.DATABASE_URL.replace("+asyncpg", ""), echo=False)

    print("=== Sprint 4: all-counties advanced trace verification ===\n")

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_before = r.json().get("balance", 0)
    print(f"Tracerfy balance: {balance_before} credits")
    print()

    county_batches: dict[str, dict] = {}

    # For each county, pull records and build payload
    for county in COUNTIES:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT ON (r.property_address)
                       r.id, r.parcel_id, r.party_name, r.property_address, r.mailing_address
                FROM results r
                JOIN jobs j ON r.job_id = j.id
                JOIN scraper_configs sc ON j.scraper_config_id = sc.id
                WHERE sc.county = :county
                  AND sc.record_type = 'probate'
                  AND r.property_address IS NOT NULL
                ORDER BY r.property_address, r.created_at DESC
                LIMIT 10
            """), {"county": county}).fetchall()

        if not rows:
            print(f"[{county}] no probate records — skipping")
            county_batches[county] = {"status": "no_data"}
            continue

        # Try to build payload rows. If some have no city, enrich via WA statewide.
        payload_rows = []
        needs_enrichment_parcels = []
        row_by_parcel = {}

        for row in rows:
            street, city, state, zipcode = extract_city_state_zip(
                row.mailing_address or "",
                row.property_address or "",
                county,
            )
            if not city and row.parcel_id:
                needs_enrichment_parcels.append(row.parcel_id)
                row_by_parcel[row.parcel_id] = row
                continue

            first_name, last_name = split_name(row.party_name)
            is_entity = classify_grantor_as_entity(row.party_name)
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

        # Fallback: enrich missing-city records via WA statewide GIS
        if needs_enrichment_parcels:
            gis_results = batch_enrich_parcels_gis(needs_enrichment_parcels, county, "WA")
            for pid, gis_data in gis_results.items():
                gis_mailing = gis_data.get("mailing_address") or ""
                parts = [p.strip() for p in gis_mailing.split(",") if p.strip()]
                if len(parts) >= 3:
                    street = parts[0]
                    city = parts[1]
                    state = "WA"
                    m = re.search(r"\b(\d{5}(?:-\d{4})?)\b", parts[2])
                    zipcode = m.group(1) if m else ""
                    if city:
                        row = row_by_parcel[pid]
                        first_name, last_name = split_name(row.party_name)
                        is_entity = classify_grantor_as_entity(row.party_name)
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

        if not payload_rows:
            print(f"[{county}] no rows with city after enrichment — skipping")
            county_batches[county] = {"status": "no_city"}
            continue

        print(f"[{county}] submitting {len(payload_rows)} advanced-trace rows...")
        try:
            response = submit_batch(payload_rows, trace_type="advanced")
        except TracerfyError as exc:
            print(f"  FAIL: {exc}")
            county_batches[county] = {"status": "submit_failed", "error": str(exc)}
            continue

        qid = response["queue_id"]
        rows_uploaded = response.get("rows_uploaded")
        print(f"  queue_id={qid}  rows_uploaded={rows_uploaded}")

        # Insert queue row for webhook ingest correlation
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
                "qid": qid,
                "uid": str(test_uid),
                "rows": rows_uploaded or 0,
                "now": datetime.now(UTC),
            })
            conn.commit()

        county_batches[county] = {
            "status": "submitted",
            "queue_id": qid,
            "submitted": len(payload_rows),
            "accepted": rows_uploaded,
        }

        # Polite pause between batch submissions (stay under 10/5min rate limit)
        time.sleep(3)

    # Wait for all queues to complete
    pending = {c: b for c, b in county_batches.items() if b.get("status") == "submitted"}
    print(f"\nWaiting for {len(pending)} webhook deliveries...")
    timeout = 240
    start = time.time()
    while pending and time.time() - start < timeout:
        time.sleep(15)
        elapsed = int(time.time() - start)
        completed_this_tick = []
        for county, batch in list(pending.items()):
            try:
                with engine.connect() as conn:
                    st = conn.execute(
                        text("SELECT status FROM skip_trace_queues WHERE tracerfy_queue_id = :qid"),
                        {"qid": batch["queue_id"]},
                    ).fetchone()
                    if st and st.status == "completed":
                        completed_this_tick.append(county)
                        batch["completed_at"] = elapsed
            except Exception:
                pass
        for c in completed_this_tick:
            del pending[c]
        print(f"  [{elapsed}s] remaining: {list(pending.keys())}")

    if pending:
        print(f"TIMEOUT — {len(pending)} queues still pending: {list(pending.keys())}")

    # Fetch per-queue CSVs and compute per-county stats directly
    print(f"\n=== Per-county scorecard ===")
    print(f"{'County':<12} {'Uploaded':>9} {'Accepted':>9} {'Phone%':>8} {'Email%':>8}  Verdict")
    print(f"{'-'*12} {'-'*9} {'-'*9} {'-'*8} {'-'*8} {'-'*30}")

    import csv, io
    from src.scrapers.enrichment.skip_trace import pick_best_phone, pick_best_email

    # One queue list fetch then lookup per queue_id
    r = requests.get(
        "https://tracerfy.com/v1/api/queues/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    all_queues = r.json()
    queue_lookup = {q["id"]: q for q in all_queues}

    for county, batch in county_batches.items():
        if batch.get("status") != "submitted":
            print(f"{county:<12} {batch.get('status', 'error'):>9}")
            continue

        qid = batch["queue_id"]
        q = queue_lookup.get(qid)
        if not q or not q.get("download_url"):
            print(f"{county:<12} {'no_url':>9}")
            continue

        csv_text = requests.get(q["download_url"], timeout=30).text
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        n = len(rows)
        phones = sum(1 for r in rows if pick_best_phone(r)[0])
        emails = sum(1 for r in rows if pick_best_email(r))

        phone_pct = 100 * phones / n if n else 0
        email_pct = 100 * emails / n if n else 0

        verdict = "PASS" if phone_pct >= 60 and email_pct >= 25 else "FAIL"
        print(f"{county:<12} {batch['submitted']:>9} {n:>9} {phone_pct:>7.0f}% {email_pct:>7.0f}%  {verdict}")

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    balance_after = r.json().get("balance", 0)
    print()
    print(f"Balance: {balance_before} -> {balance_after}  (spent {balance_before - balance_after})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
