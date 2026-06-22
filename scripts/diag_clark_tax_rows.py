"""READ-ONLY diagnostic: what ARE the Clark tax_delinquent rows in prod?

Settles the contradiction between:
  - migration 066 (2026-06-18): "Clark tax_delinquent = Federal Tax Lien only,
    0 kept (every row dropped for no parcel_id)" -> connector removed; AND
  - PR #95 (2026-06-21): backfilled real legal_description onto 1,910 of ~1,968
    Clark tax_delinquent rows by joining on a NUMERIC parcel_id (so they DO carry
    parcels).

If the rows are old (pre-066) Certificate-of-Delinquency leads with parcels +
amounts, Clark tax has real value and a re-activation should use a dedicated
trusted source. If they are parcel-less Federal Tax Liens, 066 stands and there
is nothing to re-activate. This script answers it with evidence. No writes.

Run:  railway run --service worker python scripts/diag_clark_tax_rows.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from src.db.session import SyncSessionLocal  # noqa: E402

_BASE_JOIN = """
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE lower(sc.county) = 'clark' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
"""


def _scalar(db, sql, **p):
    return db.execute(text(sql), p).scalar()


def main() -> None:
    with SyncSessionLocal() as db:
        db.execute(text("SET statement_timeout=120000"))

        total = _scalar(db, f"SELECT count(*) {_BASE_JOIN}")
        print(f"Clark tax_delinquent results (all tenants): {total}\n")
        if not total:
            print("No rows — nothing to investigate.")
            return

        # Field-presence picture: parcels/amounts/years are the crux of the 066 claim.
        for label, expr in [
            ("parcel_id NOT NULL", "r.parcel_id IS NOT NULL"),
            ("parcel_id numeric", "r.parcel_id ~ '^[0-9]+$'"),
            ("delinquent_amount NOT NULL", "r.delinquent_amount IS NOT NULL"),
            ("delinquent_bill_year NOT NULL", "r.delinquent_bill_year IS NOT NULL"),
            ("legal_description NOT NULL", "r.legal_description IS NOT NULL"),
            ("party_name NOT NULL", "r.party_name IS NOT NULL"),
            ("mailing_address NOT NULL", "r.mailing_address IS NOT NULL"),
        ]:
            n = _scalar(db, f"SELECT count(*) {_BASE_JOIN} AND {expr}")
            print(f"  {label:32} {n:6}  ({100*n/total:.1f}%)")

        print("\n  --- enrichment_data->>'source' distribution ---")
        for row in db.execute(text(
            f"SELECT r.enrichment_data->>'source' AS src, count(*) AS n {_BASE_JOIN} "
            "GROUP BY 1 ORDER BY 2 DESC"
        )):
            print(f"    source={row.src!r:45} rows={row.n}")

        print("\n  --- doc_type distribution (top 25) ---")
        for row in db.execute(text(
            f"SELECT r.doc_type AS dt, count(*) AS n {_BASE_JOIN} "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 25"
        )):
            print(f"    doc_type={row.dt!r:45} rows={row.n}")

        print("\n  --- jobs that produced these rows (scrape dates) ---")
        for row in db.execute(text(
            f"""
            SELECT j.id AS jid, j.status AS st, j.created_at AS ts,
                   sc.id AS cfg, sc.active AS cfg_active, count(*) AS n
            {_BASE_JOIN}
            GROUP BY j.id, j.status, j.created_at, sc.id, sc.active
            ORDER BY j.created_at
            """
        )):
            print(f"    job={row.jid}  {str(row.ts)[:19]}  status={row.st:8} "
                  f"cfg_active={row.cfg_active}  rows={row.n}")

        print("\n  --- sample rows (5) ---")
        for row in db.execute(text(
            f"""
            SELECT r.parcel_id, r.party_name, r.doc_type, r.delinquent_amount,
                   r.delinquent_bill_year, left(r.legal_description, 40) AS legal,
                   r.enrichment_data->>'source' AS src
            {_BASE_JOIN}
            ORDER BY r.created_at DESC LIMIT 5
            """
        )):
            print(f"    parcel={row.parcel_id} amt={row.delinquent_amount} "
                  f"yr={row.delinquent_bill_year} doc={row.doc_type!r} "
                  f"src={row.src!r} party={row.party_name!r}")

        # The scraper_configs themselves — are any still active despite 066?
        print("\n  --- clark tax_delinquent scraper_configs ---")
        for row in db.execute(text(
            """
            SELECT sc.id, sc.active, sc.batch_id IS NOT NULL AS in_batch,
                   sc.created_at, sc.updated_at
            FROM scraper_configs sc
            WHERE lower(sc.county) = 'clark' AND upper(sc.state) = 'WA'
              AND sc.record_type = 'tax_delinquent'
            ORDER BY sc.created_at
            """
        )):
            print(f"    cfg={row.id} active={row.active} in_batch={row.in_batch} "
                  f"created={str(row.created_at)[:19]} updated={str(row.updated_at)[:19]}")

        # Connector state — confirm 066 actually stripped tax_delinquent.
        print("\n  --- clark county_connectors ---")
        for row in db.execute(text(
            "SELECT county, state, record_types, scraper_class, active "
            "FROM county_connectors WHERE lower(county)='clark' AND lower(state)='wa'"
        )):
            print(f"    record_types={row.record_types} active={row.active} "
                  f"class={row.scraper_class}")


if __name__ == "__main__":
    main()
