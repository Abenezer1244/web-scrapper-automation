"""Best-effort historical backfill for results.delinquent_amount / bill_year (Phase 4).

Run MANUALLY after migration 038 — never on API boot (the results table can be
large; an in-migration backfill would brick the advisory-locked deploy).
Re-runnable + idempotent: only rows that still have BOTH structured columns NULL
are touched, so re-running never recomputes or clobbers an existing value.

SOURCE-GATED: scans only rows whose enrichment_data.source is the King Socrata
tax feed — the same gate the live worker uses — and reuses
workers.tasks._extract_tax_fields so backfill and forward paths can NEVER
diverge on coercion/validation (Codex). Other counties' tax rows have no
structured source and are skipped, staying NULL (they never match a filter).

Keyset by results.id (nil-UUID seed + CAST — never "", which Postgres rejects
for a uuid column); small batched commits.

Usage:  python scripts/backfill_result_tax_fields.py [--batch 5000]
"""
import argparse
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.workers.tasks import _extract_tax_fields

logging.basicConfig(level=logging.INFO)
# Silence SQLAlchemy per-statement echo — bulk backfill runs many statements.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
_log = logging.getLogger("backfill_result_tax_fields")

_KING_TAX_SOURCE = "king_county_delinquent_taxes"


def run(batch: int) -> None:
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    updated = 0
    skipped = 0
    while True:
        with SyncSessionLocal() as db:
            rows = db.execute(
                text(
                    """
                    SELECT id, enrichment_data
                    FROM results
                    WHERE id > CAST(:last_id AS uuid)
                      AND delinquent_amount IS NULL
                      AND delinquent_bill_year IS NULL
                      AND enrichment_data->>'source' = :src
                    ORDER BY id
                    LIMIT :batch
                    """
                ),
                {"last_id": last_id, "src": _KING_TAX_SOURCE, "batch": batch},
            ).fetchall()
            if not rows:
                break
            last_id = str(rows[-1].id)
            # ONE bulk UPDATE per batch via a VALUES join (per-row UPDATEs over a
            # remote prod connection cost a round-trip each). CAST each VALUES
            # element so an all-NULL amount/year column still has a known type.
            triples: list[tuple[str, object, object]] = []
            for row in rows:
                scanned += 1
                amount, year = _extract_tax_fields(row.enrichment_data, "tax_delinquent")
                if amount is None and year is None:
                    skipped += 1
                    continue
                triples.append((str(row.id), amount, year))
            if triples:
                values_sql = ",".join(
                    f"(:id_{i}, CAST(:amt_{i} AS numeric), CAST(:yr_{i} AS integer))"
                    for i in range(len(triples))
                )
                params: dict = {}
                for i, (rid, amt, yr) in enumerate(triples):
                    params[f"id_{i}"] = rid
                    params[f"amt_{i}"] = amt
                    params[f"yr_{i}"] = yr
                db.execute(
                    text(
                        f"""
                        UPDATE results
                        SET delinquent_amount = data.amt, delinquent_bill_year = data.yr
                        FROM (VALUES {values_sql}) AS data(id, amt, yr)
                        WHERE results.id = data.id::uuid
                          AND results.delinquent_amount IS NULL
                          AND results.delinquent_bill_year IS NULL
                        """
                    ),
                    params,
                )
                updated += len(triples)
            db.commit()
            _log.info(
                "scanned %d (updated %d, skipped %d) — through id %s",
                scanned, updated, skipped, last_id,
            )
    _log.info("done — scanned %d, updated %d, skipped %d", scanned, updated, skipped)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    run(ap.parse_args().batch)
