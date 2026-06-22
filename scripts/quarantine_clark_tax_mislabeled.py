"""Quarantine the mislabeled Clark `tax_delinquent` rows (decision: BACKLOG §9, 2026-06-21).

WHY
---
Clark `tax_delinquent` re-activation was ABANDONED (no usable property-tax source;
the Treasurer distraint list is RCW 42.56.070(8)-blocked; the recorder path yields
federal tax liens / 0 leads — migration 066). The rows that already exist in prod
are NOT tax delinquency at all: they are DEED / DEED OF TRUST / MODIFICATION recorder
documents (enrichment_data.source = 'clark_county_recorder', delinquent_amount/bill_year
0%), scraped 2026-04-10 by an immature scraper before the doc-type filter was fixed.
They sit in one Agency-plan tenant's job history + the lists/overlap index, mislabeled
as "tax delinquent". They were never delivered (delivered_records=0, dialer=0) and
carry no dedup_hash (never billed). This removes them, reversibly.

SCOPE (exact, re-asserted at commit — NEVER a blanket county delete)
--------------------------------------------------------------------
results r JOIN jobs j JOIN scraper_configs sc
  WHERE sc.record_type='tax_delinquent'
    AND lower(sc.county)='clark' AND upper(sc.state)='WA'
    AND r.enrichment_data->>'source' = 'clark_county_recorder'
Plus the matching `property_list_membership` rows scoped to record_type='tax_delinquent'
ONLY (the same parcels' probate/pre_foreclosure sightings are a DIFFERENT record_type
row and are preserved — verified live: 761 tax sightings to remove, 1,522 non-tax kept,
0 overlap with the tenant's 31,858 King tax parcels).

SAFETY MODEL (mirrors scripts/cleanup_watchdog_dup_results.py)
--------------------------------------------------------------
- DRY-RUN by default (read-only via the system session): prints the exact plan + every
  safety assertion. NOTHING changes without --commit.
- --commit REQUIRES an OWNER/admin DSN in ADMIN_DATABASE_URL_SYNC (env only, never argv):
  the worker role (bridgeleads_system) cannot DELETE from results or property_list_membership.
- Reversible: --commit first copies the full result rows + membership rows into
  server-side backup tables (_quarantine_clark_tax_results_<stamp> /
  _quarantine_clark_tax_membership_<stamp>) BEFORE any delete. `--restore <stamp>`
  re-inserts them (dynamic non-generated column list).
- Billing-safe asserts (REFUSE --commit if violated): every targeted row has
  delinquent_amount IS NULL AND delinquent_bill_year IS NULL (it really is non-tax),
  dedup_hash IS NULL (never billed), and 0 delivered_records point at it.
- Batched deletes in committed chunks; re-plan with the admin role immediately before
  deleting (the dry-run plan was built on a separate session and could be stale).

USAGE
-----
    # dry-run (read-only):
    railway run --service worker python scripts/quarantine_clark_tax_mislabeled.py

    # APPLY (needs owner DSN):
    ADMIN_DATABASE_URL_SYNC=... python scripts/quarantine_clark_tax_mislabeled.py --commit

    # RESTORE from a backup stamp:
    ADMIN_DATABASE_URL_SYNC=... python scripts/quarantine_clark_tax_mislabeled.py --restore 20260621T1530
"""
# ruff: noqa: S608 — every interpolated fragment here is either a hardcoded module
# constant (_SCOPE_WHERE), a backup-table identifier derived from a stamp validated
# against ^[A-Za-z0-9_]+$ (_STAMP_RE), or a column list built from live
# information_schema names. ALL row VALUES are passed as bound params. No user data
# reaches the SQL string.
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

# This is a ONE-TENANT, one-shape cleanup. Pin the expected blast radius so --commit
# REFUSES if the live data ever differs from what the dry-run + investigation proved
# (a newly-matching row, a second tenant, a changed count = stop, do not sweep it in).
_EXPECTED_TENANT = "46b75540-b2f7-4990-be29-05fb6f340b9d"
_EXPECTED_RESULTS = 1968
_EXPECTED_JOBS = 2
_EXPECTED_TAX_MEMBERSHIP = 761
_STAMP_RE = re.compile(r"^[A-Za-z0-9_]+$")

# The canonical "mislabeled Clark tax row" predicate. enrichment_data->>'source'
# pins it to the recorder pull; the record_type/county/state join pins it to the
# Clark tax_delinquent connector path. King/Snohomish tax use different source
# strings and are never matched.
_SCOPE_WHERE = """
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE sc.record_type = 'tax_delinquent'
      AND lower(sc.county) = 'clark' AND upper(sc.state) = 'WA'
      AND r.enrichment_data->>'source' = 'clark_county_recorder'
"""


def _scope_result_ids(db) -> list[str]:
    return [r[0] for r in db.execute(text(f"SELECT r.id::text {_SCOPE_WHERE}"))]


def _plan(db) -> dict:
    """Read-only: compute counts + run every safety assertion. Returns a dict."""
    ids = _scope_result_ids(db)
    out: dict = {"result_ids": ids, "n_results": len(ids)}
    if not ids:
        return out

    out["jobs"] = db.execute(text(f"""
        SELECT j.id::text AS jid, count(*) AS n
        {_SCOPE_WHERE} GROUP BY j.id ORDER BY 2 DESC
    """)).fetchall()
    out["tenants"] = db.execute(text(f"""
        SELECT r.user_id::text AS uid, count(*) AS n
        {_SCOPE_WHERE} GROUP BY r.user_id ORDER BY 2 DESC
    """)).fetchall()

    # Safety signals
    out["with_amount"] = db.execute(text(
        f"SELECT count(*) {_SCOPE_WHERE} AND (r.delinquent_amount IS NOT NULL "
        "OR r.delinquent_bill_year IS NOT NULL)")).scalar()
    out["with_dedup"] = db.execute(text(
        f"SELECT count(*) {_SCOPE_WHERE} AND r.dedup_hash IS NOT NULL")).scalar()
    out["delivered"] = db.execute(text(
        "SELECT count(*) FROM delivered_records "
        "WHERE first_result_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}).scalar()
    out["dialer"] = db.execute(text(
        "SELECT count(*) FROM dialer_deliveries "
        "WHERE result_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}).scalar()

    # Membership: tax_delinquent sightings for these parcels (to delete) vs the
    # non-tax sightings for the SAME parcels (must be preserved).
    out["tax_membership"] = db.execute(text(f"""
        SELECT count(*) FROM property_list_membership m
        WHERE m.record_type = 'tax_delinquent'
          AND (m.user_id, m.property_key) IN (
              SELECT r.user_id, r.property_key {_SCOPE_WHERE} AND r.property_key IS NOT NULL)
    """)).scalar()
    out["nontax_membership_same_parcels"] = db.execute(text(f"""
        SELECT count(*) FROM property_list_membership m
        WHERE m.record_type <> 'tax_delinquent'
          AND (m.user_id, m.property_key) IN (
              SELECT r.user_id, r.property_key {_SCOPE_WHERE} AND r.property_key IS NOT NULL)
    """)).scalar()
    return out


def _print_plan(p: dict) -> None:
    if not p["n_results"]:
        print("No mislabeled Clark tax rows found — nothing to do.")
        return
    print(f"Scope: {p['n_results']} result rows")
    print("  jobs:")
    for r in p["jobs"]:
        print(f"    job={r.jid}  rows={r.n}")
    print("  tenants:")
    for r in p["tenants"]:
        print(f"    user_id={r.uid}  rows={r.n}")
    print("\n  SAFETY ASSERTS (all must be benign):")
    print(f"    rows with delinquent_amount/bill_year : {p['with_amount']}   (must be 0 — proves non-tax)")
    print(f"    rows with dedup_hash (billed)          : {p['with_dedup']}   (must be 0 — proves never billed)")
    print(f"    delivered_records pointing at them     : {p['delivered']}   (must be 0)")
    print(f"    dialer_deliveries for them             : {p['dialer']}   (must be 0)")
    print(f"    tax_delinquent membership to remove    : {p['tax_membership']}")
    print(f"    NON-tax membership (same parcels) KEPT : {p['nontax_membership_same_parcels']}   (preserved — record_type-scoped delete)")


def _unsafe(p: dict) -> list[str]:
    """Hard refuse conditions for --commit. Includes the pinned blast-radius shape
    (Codex High): a count/tenant that differs from the proven dry-run means STOP."""
    bad = []
    # Content safety
    if p["with_amount"]:
        bad.append(f"{p['with_amount']} rows carry a real delinquent_amount/bill_year (NOT junk)")
    if p["with_dedup"]:
        bad.append(f"{p['with_dedup']} rows carry a dedup_hash (may have been billed)")
    if p["delivered"]:
        bad.append(f"{p['delivered']} delivered_records point at these rows")
    if p["dialer"]:
        bad.append(f"{p['dialer']} dialer_deliveries reference these rows")
    # Pinned shape — refuse if the live data drifted from the investigated scope.
    tenants = [r.uid for r in p["tenants"]]
    if tenants != [_EXPECTED_TENANT]:
        bad.append(f"tenant set {tenants} != expected [{_EXPECTED_TENANT}] (single-tenant cleanup)")
    if p["n_results"] != _EXPECTED_RESULTS:
        bad.append(f"result count {p['n_results']} != expected {_EXPECTED_RESULTS}")
    if len(p["jobs"]) != _EXPECTED_JOBS:
        bad.append(f"job count {len(p['jobs'])} != expected {_EXPECTED_JOBS}")
    if p["tax_membership"] != _EXPECTED_TAX_MEMBERSHIP:
        bad.append(f"tax membership {p['tax_membership']} != expected {_EXPECTED_TAX_MEMBERSHIP}")
    return bad


def _nongenerated_cols(db, table: str) -> list[str]:
    return [r[0] for r in db.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t "
        "AND is_generated='NEVER' ORDER BY ordinal_position"), {"t": table})]


def _commit(stamp: str) -> None:
    if not _STAMP_RE.match(stamp):
        print("REFUSING --commit: --stamp must match ^[A-Za-z0-9_]+$ (identifier-safe).")
        return
    admin_dsn = os.getenv("ADMIN_DATABASE_URL_SYNC")
    if not admin_dsn:
        print("\nREFUSING --commit: set ADMIN_DATABASE_URL_SYNC (owner/admin DSN). "
              "The worker role lacks DELETE on results + property_list_membership.")
        return
    engine = create_engine(admin_dsn, pool_pre_ping=True)
    Session = sessionmaker(engine)
    rtab = f"_quarantine_clark_tax_results_{stamp}"
    mtab = f"_quarantine_clark_tax_membership_{stamp}"
    jtab = f"_quarantine_clark_tax_jobs_{stamp}"

    # Everything in ONE transaction (Codex High): backup the EXACT rows, then delete
    # ONLY from those backup tables, so delete-set == backup-set by construction (no
    # TOCTOU between backup, membership-delete, result-delete). DDL is transactional in
    # Postgres, so a failure rolls the whole thing back (backup tables included).
    with Session() as db:
        p = _plan(db)
        if not p["n_results"]:
            print("Re-plan: nothing to quarantine.")
            return
        bad = _unsafe(p)
        if bad:
            print("REFUSING --commit — asserts failed (state changed since dry-run?):")
            for b in bad:
                print(f"  - {b}")
            return

        # 1) Backup EXACT sets. CREATE TABLE (no IF NOT EXISTS) → fails if the stamp
        #    was already used, so we never silently reuse a stale backup.
        db.execute(text(f'CREATE TABLE "{rtab}" AS '
                        f"SELECT r.* {_SCOPE_WHERE}"))
        # Membership is keyed (user_id, record_type, property_key) — ONE row per tuple
        # — and property_key is COUNTY/STATE-scoped (compute_property_key), so a Clark
        # parcel's key can never collide with King/Snohomish. This tenant's Clark
        # tax_delinquent sightings came ONLY from these 2 jobs (Clark tax was never
        # scraped otherwise), and a live check showed 0 overlap with their 31,858 King
        # tax parcels. So every tax_delinquent membership at these property_keys IS a
        # mislabeled-Clark sighting. The _EXPECTED_TAX_MEMBERSHIP=761 pin + the
        # delete-set==backup-set join below enforce it; a drift aborts the whole txn.
        db.execute(text(f'CREATE TABLE "{mtab}" AS '
                        "SELECT m.* FROM property_list_membership m "
                        "WHERE m.record_type='tax_delinquent' "
                        f"AND (m.user_id, m.property_key) IN (SELECT r.user_id, r.property_key {_SCOPE_WHERE} "
                        "  AND r.property_key IS NOT NULL)"))
        db.execute(text(f'CREATE TABLE "{jtab}" AS '
                        "SELECT DISTINCT j.id, j.record_count FROM jobs j "
                        "JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
                        "WHERE sc.record_type='tax_delinquent' "
                        "AND lower(sc.county)='clark' AND upper(sc.state)='WA' "
                        f"AND j.id IN (SELECT r.job_id {_SCOPE_WHERE})"))
        nb = db.execute(text(f'SELECT count(*) FROM "{rtab}"')).scalar()
        nm = db.execute(text(f'SELECT count(*) FROM "{mtab}"')).scalar()
        if nb != _EXPECTED_RESULTS or nm != _EXPECTED_TAX_MEMBERSHIP:
            raise RuntimeError(f"backup count mismatch (results {nb}/{_EXPECTED_RESULTS}, "
                               f"membership {nm}/{_EXPECTED_TAX_MEMBERSHIP}) — rolled back")

        # 2) Delete ONLY what is in the backup tables (exact-key, no predicate drift).
        dm = db.execute(text(
            f'DELETE FROM property_list_membership m USING "{mtab}" b '
            "WHERE m.user_id=b.user_id AND m.record_type=b.record_type "
            "AND m.property_key=b.property_key")).rowcount
        dr = db.execute(text(
            f'DELETE FROM results r USING "{rtab}" b WHERE r.id=b.id')).rowcount
        if dr != nb or dm != nm:
            raise RuntimeError(f"delete count != backup (results {dr}/{nb}, membership {dm}/{nm}) — rolled back")

        # 3) Zero record_count on the now-empty jobs (history consistency).
        dj = db.execute(text(
            f'UPDATE jobs SET record_count=0 FROM "{jtab}" b '
            "WHERE jobs.id=b.id AND jobs.record_count<>0 "
            "AND NOT EXISTS (SELECT 1 FROM results r WHERE r.job_id=jobs.id)")).rowcount

        db.commit()
        print(f"COMMITTED (one txn): backed up {nb} results + {nm} memberships + job counts; "
              f"deleted {dr} results + {dm} memberships; zeroed {dj} job(s).")
        print(f"Restore with: --restore {stamp}")


def _restore(stamp: str) -> None:
    if not _STAMP_RE.match(stamp):
        print("REFUSING --restore: --stamp must match ^[A-Za-z0-9_]+$.")
        return
    admin_dsn = os.getenv("ADMIN_DATABASE_URL_SYNC")
    if not admin_dsn:
        print("REFUSING --restore: set ADMIN_DATABASE_URL_SYNC (owner/admin DSN).")
        return
    engine = create_engine(admin_dsn, pool_pre_ping=True)
    Session = sessionmaker(engine)
    rtab = f"_quarantine_clark_tax_results_{stamp}"
    mtab = f"_quarantine_clark_tax_membership_{stamp}"
    jtab = f"_quarantine_clark_tax_jobs_{stamp}"

    def _collist(db, live_table: str, backup_table: str) -> str:
        live = _nongenerated_cols(db, live_table)
        have = {r[0] for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"), {"t": backup_table})}
        return ", ".join(f'"{c}"' for c in live if c in have)

    with Session() as db:
        rcl = _collist(db, "results", rtab)
        n = db.execute(text(
            f'INSERT INTO results ({rcl}) SELECT {rcl} FROM "{rtab}" '
            "ON CONFLICT (id) DO NOTHING")).rowcount
        mcl = _collist(db, "property_list_membership", mtab)
        m = db.execute(text(
            f'INSERT INTO property_list_membership ({mcl}) SELECT {mcl} FROM "{mtab}" '
            "ON CONFLICT DO NOTHING")).rowcount
        # Restore the original job record_count too (commit zeroed it).
        j = db.execute(text(
            f'UPDATE jobs SET record_count=b.record_count FROM "{jtab}" b WHERE jobs.id=b.id')).rowcount
        db.commit()
        print(f"Restored {n} result rows + {m} membership rows + {j} job count(s) from stamp {stamp}.")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--commit", action="store_true", help="apply (needs owner DSN)")
    g.add_argument("--restore", metavar="STAMP", help="restore from a backup stamp")
    ap.add_argument("--stamp", default=None,
                    help="backup-table suffix for --commit (default: derived; pass to keep deterministic)")
    args = ap.parse_args()

    if args.restore:
        _restore(args.restore)
        return

    with system_sync_session() as db:
        p = _plan(db)
        _print_plan(p)
        if not p["n_results"]:
            return
        bad = _unsafe(p)
        if bad:
            print("\n*** SAFETY ASSERTS FAILED — would REFUSE --commit: ***")
            for b in bad:
                print(f"  - {b}")

    if not args.commit:
        print("\nDRY-RUN — nothing changed. Re-run with --commit + ADMIN_DATABASE_URL_SYNC to apply.")
        return

    # --stamp must be provided for a deterministic, restorable backup-table name
    # (Date.now()-style auto-stamps are avoided so the operator owns the name).
    if not args.stamp:
        print("\n--commit requires --stamp <id> (e.g. 20260621T1530) so the backup "
              "tables have a known, restorable name.")
        return
    _commit(args.stamp)


if __name__ == "__main__":
    main()
