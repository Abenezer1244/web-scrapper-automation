"""Cross-list overlap + structured-tax-field helpers, extracted from tasks.py.

Holds the property-membership rollup, the results.property_key stamp, and the
source-gated tax-field extraction. Moved verbatim — behavior is byte-identical
to the originals in tasks.py.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError

from src.utils.logger import setup_logger
from src.workers.property_identity import compute_property_key as _compute_property_key

_logger = setup_logger("worker.task")


def _upsert_property_membership(
    db, rows, user_id: str, record_type: str, county: str | None, state: str | None
) -> int:
    """Phase 1: roll up strong-identity property sightings for cross-list overlap.

    `rows` = post-enrichment Result objects (only .parcel_id / .property_address
    are read). Pre-aggregates by property_key in Python so a single multi-row
    INSERT never hits the same conflict key twice ("cannot affect row a second
    time"). Deadlock-ordered by property_key; retried on serialization/deadlock.
    Returns the number of distinct strong properties upserted.

    Advisory only: sighting_count is not idempotent across job re-runs. Failures
    are caller-handled — this never participates in billing.
    """
    agg: dict[str, dict] = {}
    for res in rows:
        # County/state-scoped overlap key (2026-06-12) — config context required.
        key = _compute_property_key(res.parcel_id, res.property_address, county, state)
        if not key:
            continue
        cur = agg.get(key)
        if cur is None:
            agg[key] = {
                "parcel_id": (res.parcel_id or None),
                "property_address": (res.property_address or None),
                "count": 1,
            }
        else:
            cur["count"] += 1
            cur["parcel_id"] = cur["parcel_id"] or res.parcel_id
            cur["property_address"] = cur["property_address"] or res.property_address
    if not agg:
        return 0

    items = sorted(agg.items())  # deterministic lock order (deadlock guard)
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]
        values_sql = ",".join(
            f"(:uid_{k}, :rt_{k}, :pk_{k}, :pid_{k}, :addr_{k}, :cnt_{k}, NOW(), NOW())"
            for k in range(len(chunk))
        )
        params: dict = {}
        for k, (key, v) in enumerate(chunk):
            params[f"uid_{k}"] = user_id
            params[f"rt_{k}"] = record_type
            params[f"pk_{k}"] = key
            params[f"pid_{k}"] = (v["parcel_id"] or None)
            params[f"addr_{k}"] = (v["property_address"] or None)
            params[f"cnt_{k}"] = v["count"]
        stmt = sa_text(f"""
            INSERT INTO property_list_membership
                (user_id, record_type, property_key, parcel_id,
                 property_address, sighting_count, first_seen_at, last_seen_at)
            VALUES {values_sql}
            ON CONFLICT (user_id, record_type, property_key) DO UPDATE SET
                sighting_count   = property_list_membership.sighting_count + EXCLUDED.sighting_count,
                first_seen_at    = LEAST(property_list_membership.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at     = GREATEST(property_list_membership.last_seen_at, EXCLUDED.last_seen_at),
                parcel_id        = COALESCE(property_list_membership.parcel_id, EXCLUDED.parcel_id),
                property_address = COALESCE(property_list_membership.property_address, EXCLUDED.property_address)
        """)
        for attempt in range(3):
            try:
                db.execute(stmt, params)
                db.commit()
                break
            except OperationalError as exc:
                db.rollback()
                # psycopg2 (this stack) exposes SQLSTATE as .pgcode, NOT .sqlstate.
                pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
                if pgcode not in ("40001", "40P01") or attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    return len(agg)


def _write_result_property_keys(
    db, rows, user_id: str, county: str | None, state: str | None
) -> tuple[int, int]:
    """Phase 3: stamp results.property_key on post-enrichment rows.

    property_key is the SAME strong-identity key membership stores
    (compute_property_key) — it lets the combine/overlap export join overlap
    property_keys back to full Result rows. Computed here, in the same
    post-enrichment spot as the membership rollup, so enrichment-resolved
    parcels/addresses are reflected.

    `rows` = post-enrichment Result objects (only .id / .parcel_id /
    .property_address are read). We do NOT mutate the ORM objects — an
    attribute-set would make the shared session dirty and a stray autoflush
    could push writes before the membership block commits, or poison the
    session on error (Codex review). Instead: an explicit bulk UPDATE by id in
    its OWN transaction, idempotent via `property_key IS NULL` (never clobbers a
    value, safe on task retry). Returns (updated_count, weak_skipped_count).

    Failure-isolated by the caller (like membership): on hard failure we never
    fail an already-delivered job; scripts/backfill_result_property_key.py heals.
    """
    pairs: list[tuple[str, str]] = []
    weak = 0
    for res in rows:
        # County/state-scoped overlap key (2026-06-12) — config context required.
        key = _compute_property_key(res.parcel_id, res.property_address, county, state)
        if not key:
            weak += 1
            continue
        pairs.append((str(res.id), key))
    if not pairs:
        return (0, weak)

    updated = 0
    for i in range(0, len(pairs), 500):
        chunk = pairs[i:i + 500]
        values_sql = ",".join(f"(:id_{k}, :pk_{k})" for k in range(len(chunk)))
        params: dict = {"uid": user_id}
        for k, (rid, key) in enumerate(chunk):
            params[f"id_{k}"] = rid
            params[f"pk_{k}"] = key
        # UPDATE ... FROM (VALUES ...) — one statement per chunk (no N+1).
        # data.id::uuid casts the bound text to the column type. The
        # property_key IS NULL guard makes this idempotent on re-run.
        stmt = sa_text(f"""
            UPDATE results
            SET property_key = data.pk
            FROM (VALUES {values_sql}) AS data(id, pk)
            WHERE results.id = data.id::uuid
              AND results.user_id = :uid
              AND results.property_key IS NULL
        """)
        res_proxy = db.execute(stmt, params)
        db.commit()
        updated += res_proxy.rowcount or 0
    return (updated, weak)


# Scrapers whose tax_delinquent rows carry trustworthy structured
# delinquent_amount + bill_year (the only sources _extract_tax_fields trusts).
# Adding a county = add its exact source string here AFTER confirming the scraper
# emits a real bill year + a clean owed amount. NEVER widen this to a generic
# "if the keys exist" check — that would mis-populate any scraper reusing the
# key names with a different meaning (Codex).
_TRUSTED_TAX_SOURCES = frozenset({
    "king_county_delinquent_taxes",        # King — Socrata API
    "snohomish_county_delinquent_taxes",   # Snohomish — Treasurer bulk Current Tax List
})


def _extract_tax_fields(
    enrichment_data, record_type: str
) -> tuple[Decimal | None, int | None]:
    """Phase 4: SOURCE-GATED structured tax fields for amount/age filtering.

    Returns (delinquent_amount, bill_year). ONLY scrapers in
    ``_TRUSTED_TAX_SOURCES`` carry trustworthy structured data, so anything else
    returns (None, None) and never matches a tax filter — a generic "if the keys
    exist" extraction would mis-populate any future scraper that reuses those key
    names with a different meaning (Codex). Values are coerced + bounded so a
    malformed scrape can't poison the filter columns. Raw enrichment_data is
    untouched.
    """
    if record_type != "tax_delinquent" or not isinstance(enrichment_data, dict):
        return (None, None)
    if enrichment_data.get("source") not in _TRUSTED_TAX_SOURCES:
        return (None, None)

    amount: Decimal | None = None
    raw_amt = enrichment_data.get("delinquent_amount")
    if raw_amt is not None:
        try:
            # Decimal(str(...)) — never Decimal(float) (binary-float drift).
            d = Decimal(str(raw_amt)).quantize(Decimal("0.01"))
            if d.is_finite() and Decimal("0") <= d <= Decimal("99999999.99"):
                amount = d
        except (InvalidOperation, ValueError, TypeError):
            amount = None

    year: int | None = None
    raw_year = enrichment_data.get("bill_year")
    if raw_year not in (None, ""):
        try:
            y = int(str(raw_year).strip())
            if 1900 <= y <= datetime.now(UTC).year + 1:
                year = y
        except (ValueError, TypeError):
            year = None

    return (amount, year)
