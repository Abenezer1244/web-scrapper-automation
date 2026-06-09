"""Enqueue skip-trace rows for existing Thurston/Kitsap/Whatcom records.

Rather than re-scraping (which the earlier run crashed on sa_select
NameError leaving the records with skip_trace_status='not_attempted'),
this script calls the fixed _enqueue_skip_trace_rows logic directly on
the existing Result rows. The dispatcher then picks them up on its
next 5-min tick and submits to Tracerfy.
"""
import os
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from dotenv import load_dotenv
load_dotenv(".env")

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import (
    Job,
    PendingSkipTraceRow,
    Result,
    ScraperConfig,
    SkipTraceCache,
    User,
)
from src.scrapers.enrichment.skip_trace import (
    address_cache_key,
    build_pending_row_payload,
)
from src.utils.crypto import encrypt_field, is_encrypted

COUNTIES = ["thurston", "kitsap", "whatcom"]


def _enc_pii(value):
    """H3: ensure a contact value written via raw SQL into an EncryptedString
    column is stored as ciphertext. The raw text() UPDATE here bypasses the
    TypeDecorator, and the source SkipTraceCache value (also read raw) may be
    plaintext (pre-backfill) or already fe1: ciphertext. Normalize: blank->NULL,
    already-encrypted->keep, plaintext->encrypt. Mirrors EncryptedString."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return value if is_encrypted(value) else encrypt_field(value)


def main() -> int:
    engine = create_engine(settings.DATABASE_URL.replace("+asyncpg", ""), echo=False)

    print("=== Enqueue existing Thurston/Kitsap/Whatcom records ===\n")

    total_queued = 0
    total_cache_hits = 0

    with Session(engine) as db:
        for county in COUNTIES:
            # Find the most recent job for this county
            job = db.execute(text("""
                SELECT j.id, j.user_id
                FROM jobs j
                JOIN scraper_configs sc ON j.scraper_config_id = sc.id
                WHERE LOWER(sc.county) = :county
                  AND sc.record_type = 'probate'
                  AND j.status = 'done'
                ORDER BY j.created_at DESC
                LIMIT 1
            """), {"county": county}).fetchone()

            if not job:
                print(f"[{county}] no completed job found — skipping")
                continue

            # Pull all Results with skip_trace_status='not_attempted'
            results = db.execute(text("""
                SELECT id, job_id, user_id, party_name, property_address, mailing_address, parcel_id
                FROM results
                WHERE job_id = :jid
                  AND skip_trace_status = 'not_attempted'
                  AND property_address IS NOT NULL
            """), {"jid": str(job.id)}).fetchall()

            print(f"[{county}] job {str(job.id)[:8]} — {len(results)} not_attempted records")

            if not results:
                continue

            queued = 0
            hits = 0
            # Use a lightweight Result-like object to feed build_pending_row_payload
            class _ResultRow:
                pass
            for r in results:
                stub = _ResultRow()
                stub.id = r.id
                stub.job_id = r.job_id
                stub.user_id = r.user_id
                stub.party_name = r.party_name
                # Use mailing_address if present (has city/state/zip),
                # fallback to property_address
                stub.property_address = r.mailing_address if (r.mailing_address and "," in r.mailing_address) else r.property_address

                payload = build_pending_row_payload(stub)
                if payload is None:
                    continue

                # Cache lookup
                cache_key = address_cache_key(
                    payload["property_address"],
                    payload["city"],
                    payload["state"],
                )
                cached = db.execute(text("""
                    SELECT phone, phone_type, email, fetched_at
                    FROM skip_trace_cache
                    WHERE address_hash = :k
                """), {"k": cache_key}).fetchone()

                cache_valid = False
                if cached:
                    age = datetime.now(UTC) - cached.fetched_at
                    if age.days < settings.SKIP_TRACE_CACHE_DAYS:
                        cache_valid = True

                if cache_valid:
                    # Hit — update Result directly
                    db.execute(text("""
                        UPDATE results
                        SET phone = :p, phone_type = :pt, email = :e,
                            skip_trace_status = :s, skip_trace_attempted_at = :now
                        WHERE id = :id
                    """), {
                        "p": _enc_pii(cached.phone),
                        "pt": cached.phone_type,
                        "e": _enc_pii(cached.email),
                        "s": "hit" if (cached.phone or cached.email) else "miss",
                        "now": datetime.now(UTC),
                        "id": r.id,
                    })
                    hits += 1
                else:
                    # Miss — enqueue for the dispatcher
                    db.execute(text("""
                        INSERT INTO pending_skip_trace_rows
                            (id, job_id, result_id, user_id,
                             property_address, city, state, zip,
                             first_name, last_name,
                             mail_address, mail_city, mail_state, mail_zip,
                             trace_type, status, enqueued_at)
                        VALUES
                            (gen_random_uuid(), :job_id, :result_id, :user_id,
                             :property_address, :city, :state, :zip,
                             :first_name, :last_name,
                             :mail_address, :mail_city, :mail_state, :mail_zip,
                             :trace_type, 'queued', :enqueued_at)
                    """), {
                        "job_id": payload["job_id"],
                        "result_id": payload["result_id"],
                        "user_id": payload["user_id"],
                        "property_address": payload["property_address"],
                        "city": payload["city"],
                        "state": payload["state"],
                        "zip": payload["zip"],
                        "first_name": payload["first_name"],
                        "last_name": payload["last_name"],
                        "mail_address": payload["mail_address"],
                        "mail_city": payload["mail_city"],
                        "mail_state": payload["mail_state"],
                        "mail_zip": payload["mail_zip"],
                        "trace_type": payload["trace_type"],
                        "enqueued_at": datetime.now(UTC),
                    })
                    # Mark Result as queued
                    db.execute(text("""
                        UPDATE results
                        SET skip_trace_status = 'queued'
                        WHERE id = :id
                    """), {"id": r.id})
                    queued += 1

            db.commit()
            print(f"[{county}] queued={queued}  cache_hits={hits}")
            total_queued += queued
            total_cache_hits += hits

    print()
    print(f"Total queued: {total_queued}  cache_hits: {total_cache_hits}")
    print()
    print("Dispatcher runs every 5 min. Waiting up to 12 min for first submission...")

    # Poll for dispatcher submission
    start = time.time()
    while time.time() - start < 12 * 60:
        time.sleep(20)
        elapsed = int(time.time() - start)
        with engine.connect() as conn:
            stats = conn.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'queued') as queued,
                    COUNT(*) FILTER (WHERE status = 'submitted') as submitted,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed
                FROM pending_skip_trace_rows
                WHERE enqueued_at >= NOW() - INTERVAL '30 minutes'
            """)).fetchone()
        print(f"  [{elapsed}s] pending={stats.queued}  submitted={stats.submitted}  completed={stats.completed}")
        if stats.queued == 0 and stats.submitted == 0:
            print("  All pending rows resolved")
            break

    # Final scorecard
    print()
    print("=== Final per-county scorecard ===")
    with engine.connect() as conn:
        for county in COUNTIES:
            stats = conn.execute(text("""
                SELECT COUNT(*) as total,
                       COUNT(r.phone) as with_phone,
                       COUNT(r.email) as with_email
                FROM results r
                JOIN jobs j ON r.job_id = j.id
                JOIN scraper_configs sc ON j.scraper_config_id = sc.id
                WHERE LOWER(sc.county) = :county
                  AND sc.record_type = 'probate'
                  AND j.status = 'done'
                  AND r.property_address IS NOT NULL
                  AND r.created_at >= NOW() - INTERVAL '2 hours'
            """), {"county": county}).fetchone()
            n = stats.total
            p = stats.with_phone
            e = stats.with_email
            if n:
                p_pct = 100 * p / n
                e_pct = 100 * e / n
                verdict = "PASS" if p_pct >= 60 and e_pct >= 25 else "PARTIAL"
                print(f"[{county}] {n} records  phone={p}/{n} ({p_pct:.0f}%)  email={e}/{n} ({e_pct:.0f}%)  {verdict}")

    r = requests.get(
        "https://tracerfy.com/v1/api/analytics/",
        headers={"Authorization": f"Bearer {settings.TRACERFY_API_TOKEN}"},
        timeout=15,
    )
    print(f"\nTracerfy balance: {r.json().get('balance')} credits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
