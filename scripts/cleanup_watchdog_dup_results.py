"""Historical cleanup of watchdog-appended DUPLICATE result rows (2026-06-17 incident).

The wall-clock watchdog re-queued LIVE long jobs; the non-idempotent re-run
re-scraped and APPENDED a second (third, ...) full copy of the job's results.
This removes those appended copies — keeping exactly ONE row per distinct scraped
record per job — without ever touching legitimately-distinct rows (e.g. multiple
probate filings on one parcel).

SAFETY MODEL
------------
- DRY-RUN by default (read-only): reports what WOULD be deleted per job + sanity
  checks. NOTHING is deleted without --commit.
- Grouping key = the row's SCRAPE-TIME content identity:
      coalesce(raw_html_hash, md5(canonical tuple of scrape-stable fields))
  It EXCLUDES property_address / mailing_address / enrichment_data because
  enrichment may have filled them on one copy and not another — a watchdog copy
  is identical at INSERT, so the scrape-stable key groups original+copies even
  after divergent enrichment. Genuinely-distinct leads differ in a scrape field
  (doc_type/date/party/parcel/tax) and so are NEVER grouped/removed.
- Survivor per group (keep one, delete the rest), in priority order:
      1. referenced by delivered_records.first_result_id  (billing/delivery anchor)
      2. is_duplicate = false  (the originally-delivered row)
      3. has mailing_address   4. has property_address
      5. skip_trace_status='hit'   6. has property_key   7. earliest created_at, id
- Before deleting a row that delivered_records.first_result_id points at, the
  pointer is RE-POINTED to the survivor (the FK is ON DELETE SET NULL, which would
  otherwise orphan the billing→result link).
- Billing: the appended copies were marked is_duplicate=true by the re-run's dedup
  (their hashes already claimed), so records_used was NOT double-counted; deleting
  is_duplicate=true rows needs no quota adjustment. The dry-run FLAGS any group
  whose deletions include an is_duplicate=false row so a human checks before commit.

GRANTS: --commit DELETEs from results + UPDATEs delivered_records, which the worker
role (bridgeleads_system) cannot do. Provide an OWNER/admin sync DSN via
--admin-dsn or the ADMIN_DATABASE_URL_SYNC env var. DRY-RUN uses the normal
system session (reads only).

USAGE
-----
    # dry-run, auto-detected integer-multiple suspects (safe default scope):
    railway run --service worker python scripts/cleanup_watchdog_dup_results.py

    # dry-run, explicit jobs:
    railway run --service worker python scripts/cleanup_watchdog_dup_results.py --ids <id1> <id2>

    # dry-run, ALL 202 dup jobs (incl. ambiguous):
    railway run --service worker python scripts/cleanup_watchdog_dup_results.py --all

    # APPLY (requires owner DSN):
    ADMIN_DATABASE_URL_SYNC=... python scripts/cleanup_watchdog_dup_results.py --ids <id> --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

BATCH_SIZE = 500
TERMINAL_JOB_STATUSES = ("done", "failed", "cancelled")

# Scrape-stable content identity (NO enriched fields: property/mailing/enrichment
# drift on enrichment). raw_html_hash is COMBINED WITH the tuple, never chosen
# instead of it (Codex Critical): if a scraper's raw_html_hash were page/batch-level
# rather than record-unique, choosing it alone would collapse distinct leads. As one
# more discriminator inside the tuple it can only INCREASE distinctness → never
# over-deletes. jsonb_build_array gives a structured, NULL- and delimiter-safe
# encoding (no "|"-collision); ::text casts keep date/numeric columns well-typed.
_CFP_SQL = (
    "md5(jsonb_build_array("
    "nullif(r.raw_html_hash, ''), r.parcel_id, r.party_name, "
    "r.date_recorded::text, r.doc_type, r.legal_description, r.dedup_hash, "
    "r.delinquent_amount::text, r.delinquent_bill_year::text"
    ")::text)"
)

# Per (job, content) survivor ranking; rn=1 is kept, rn>1 deleted.
# _CFP_SQL is a hardcoded module constant (no user input) — not an injection vector.
_RANK_SQL = f"""
    SELECT r.id::text AS id, r.job_id::text AS job_id, r.is_duplicate,
           (dr.first_result_id IS NOT NULL) AS is_anchor,
           {_CFP_SQL} AS cfp,
           row_number() OVER (
               PARTITION BY r.job_id, {_CFP_SQL}
               ORDER BY (dr.first_result_id IS NOT NULL) DESC,
                        r.is_duplicate ASC,
                        (r.mailing_address IS NOT NULL) DESC,
                        (r.property_address IS NOT NULL) DESC,
                        (r.skip_trace_status = 'hit') DESC,
                        (r.property_key IS NOT NULL) DESC,
                        r.created_at ASC, r.id ASC
           ) AS rn
    FROM results r
    LEFT JOIN delivered_records dr ON dr.first_result_id = r.id
    WHERE r.job_id = ANY(CAST(:job_ids AS uuid[]))
"""  # noqa: S608 — _CFP_SQL is a constant, :job_ids is bound; no injection vector


def _autodetect_integer_multiple_jobs(db) -> list[str]:
    """Jobs whose rows are a near-integer multiple of distinct parcels — the clear
    watchdog-append signature (a full set re-appended N times). Excludes the
    x1.0x-x1.1x legit multi-lead-per-parcel noise."""
    rows = db.execute(
        text(
            """
            SELECT j.id::text AS id,
                   count(r.id) AS rows, count(DISTINCT r.parcel_id) AS parcels
            FROM jobs j JOIN results r ON r.job_id = j.id
            WHERE r.parcel_id IS NOT NULL
            GROUP BY j.id
            HAVING count(r.id) > count(DISTINCT r.parcel_id) * 1.4
            ORDER BY count(r.id) DESC
            """
        )
    ).fetchall()
    return [row.id for row in rows]


def _resolve_job_ids(db, args) -> list[str]:
    if args.ids:
        return args.ids
    if args.all:
        rows = db.execute(
            text(
                """
                SELECT j.id::text AS id FROM jobs j JOIN results r ON r.job_id = j.id
                WHERE r.parcel_id IS NOT NULL
                GROUP BY j.id HAVING count(r.id) > count(DISTINCT r.parcel_id)
                """
            )
        ).fetchall()
        return [row.id for row in rows]
    return _autodetect_integer_multiple_jobs(db)


def _plan(db, job_ids: list[str]) -> dict:
    """Compute the deletion plan (read-only). Returns per-job stats + flags +
    the exact (doomed_anchor_id -> survivor_id) re-point map for --commit."""
    ranked = db.execute(text(_RANK_SQL), {"job_ids": job_ids}).fetchall()
    # First pass: survivor id per (job, content-fingerprint).
    survivor: dict[tuple[str, str], str] = {}
    for row in ranked:
        if row.rn == 1:
            survivor[(row.job_id, row.cfp)] = row.id
    # Second pass: classify, and map each doomed ANCHOR to its group survivor.
    by_job: dict[str, dict] = {}
    for row in ranked:
        j = by_job.setdefault(
            row.job_id,
            {"total": 0, "keep": 0, "delete": 0, "delete_ids": [],
             "delete_nondup": 0, "repoint": []},
        )
        j["total"] += 1
        if row.rn == 1:
            j["keep"] += 1
            continue
        j["delete"] += 1
        j["delete_ids"].append(row.id)
        if not row.is_duplicate:
            j["delete_nondup"] += 1       # FLAG: deleting an originally-billed row
        if row.is_anchor:
            # Re-point delivered_records from this doomed row to ITS group survivor.
            j["repoint"].append((row.id, survivor[(row.job_id, row.cfp)]))
    return by_job


def _chunks(values: list[str], size: int = BATCH_SIZE):
    for k in range(0, len(values), size):
        yield values[k:k + size]


def _assert_terminal_jobs(db, job_ids: list[str]) -> None:
    rows = db.execute(
        text(
            """
            SELECT id::text AS id, status
            FROM jobs
            WHERE id = ANY(CAST(:job_ids AS uuid[]))
            """
        ),
        {"job_ids": job_ids},
    ).fetchall()
    found = {row.id for row in rows}
    missing = sorted(set(job_ids) - found)
    non_terminal = [
        f"{row.id}:{row.status}" for row in rows
        if row.status not in TERMINAL_JOB_STATUSES
    ]
    if missing or non_terminal:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if non_terminal:
            details.append(f"non_terminal={non_terminal}")
        raise RuntimeError("refusing cleanup for unsafe job scope: " + "; ".join(details))


def _assert_no_delivered_anchors(db, ids: list[str], context: str) -> None:
    still = db.execute(
        text(
            "SELECT count(*) FROM delivered_records "
            "WHERE first_result_id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": ids},
    ).scalar()
    if still:
        raise RuntimeError(
            f"{still} delivered_records still point at to-delete rows ({context})"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--ids", nargs="+", help="explicit job ids")
    g.add_argument("--all", action="store_true", help="ALL dup jobs (incl. ambiguous)")
    ap.add_argument("--commit", action="store_true", help="apply (needs owner DSN)")
    args = ap.parse_args()
    # Env-only (never argv — a DSN in argv leaks via process listings). The owner/
    # admin role is required: the worker role (bridgeleads_system) lacks DELETE on
    # results + UPDATE on delivered_records.
    admin_dsn = os.getenv("ADMIN_DATABASE_URL_SYNC")

    with system_sync_session() as db:
        job_ids = _resolve_job_ids(db, args)
        if not job_ids:
            print("No matching jobs.")
            return
        print(f"Scope: {len(job_ids)} job(s).")
        plan = _plan(db, job_ids)

    total_del = sum(j["delete"] for j in plan.values())
    total_nondup = sum(j["delete_nondup"] for j in plan.values())
    total_anchor = sum(len(j["repoint"]) for j in plan.values())
    for jid, j in sorted(plan.items(), key=lambda kv: -kv[1]["delete"]):
        if j["delete"] == 0:
            continue
        flags = []
        if j["delete_nondup"]:
            flags.append(f"!NONDUP={j['delete_nondup']}")
        if j["repoint"]:
            flags.append(f"anchor-repoint={len(j['repoint'])}")
        print(f"  {jid} total={j['total']} keep={j['keep']} "
              f"delete={j['delete']} {' '.join(flags)}")
    print(f"\nTOTAL would delete {total_del} rows across "
          f"{sum(1 for j in plan.values() if j['delete'])} jobs. "
          f"NONDUP-deletes={total_nondup} (should be 0 — investigate if not), "
          f"anchor-repoints={total_anchor}.")

    if not args.commit:
        print("\nDRY-RUN — no rows deleted. Re-run with --commit + owner DSN to apply.")
        return

    if total_nondup:
        print(f"\nREFUSING --commit: {total_nondup} non-duplicate rows would be "
              "deleted. Investigate first (billing implication).")
        return
    if not admin_dsn:
        print("\nREFUSING --commit: set ADMIN_DATABASE_URL_SYNC (owner/admin DSN). "
              "The worker role lacks DELETE on results + UPDATE on delivered_records.")
        return

    admin_engine = create_engine(admin_dsn, pool_pre_ping=True)
    AdminSession = sessionmaker(admin_engine)
    deleted = 0
    repointed = 0
    done_jobs: list[str] = []
    failed_jobs: list[tuple[str, str]] = []
    with AdminSession() as adb:
        # RECOMPUTE the plan with the admin role immediately before deleting
        # (Codex High): the dry-run plan was built on a separate (system) session
        # and could be stale. Then end this read-only transaction before writes.
        _assert_terminal_jobs(adb, job_ids)
        fresh = _plan(adb, job_ids)
        fresh_nondup = sum(v["delete_nondup"] for v in fresh.values())
        if fresh_nondup:
            print(f"REFUSING --commit: re-plan now shows {fresh_nondup} non-duplicate "
                  "deletes (state changed since dry-run). Re-run dry-run.")
            return
        adb.commit()

    for jid, j in fresh.items():
        ids = j["delete_ids"]
        if not ids:
            continue
        job_deleted = 0
        job_repointed = 0
        try:
            # Commit anchor re-points before any deletes. If the process dies after
            # this point, delivered_records are in the safer state and a rerun will
            # recompute a smaller/no-op repoint map.
            with AdminSession() as adb:
                for doomed_id, survivor_id in j["repoint"]:
                    job_repointed += adb.execute(
                        text(
                            "UPDATE delivered_records SET first_result_id = CAST(:s AS uuid) "
                            "WHERE first_result_id = CAST(:d AS uuid)"
                        ),
                        {"s": survivor_id, "d": doomed_id},
                    ).rowcount
                _assert_no_delivered_anchors(adb, ids, f"job {jid} after re-point")
                adb.commit()
                repointed += job_repointed

            # Delete in short committed batches. This bounds lock duration and WAL
            # exposure; partial progress is intentionally restartable.
            for chunk in _chunks(ids):
                with AdminSession() as adb:
                    _assert_no_delivered_anchors(adb, chunk, f"job {jid} batch")
                    batch_deleted = adb.execute(
                        text("DELETE FROM results WHERE id = ANY(CAST(:ids AS uuid[]))"),
                        {"ids": chunk},
                    ).rowcount
                    if batch_deleted != len(chunk):
                        raise RuntimeError(
                            f"deleted {batch_deleted} != planned batch {len(chunk)}"
                        )
                    adb.commit()
                    job_deleted += batch_deleted
                    deleted += batch_deleted

            if job_deleted != len(ids):
                raise RuntimeError(
                    f"deleted {job_deleted} != planned {len(ids)} for job {jid}"
                )
            done_jobs.append(jid)
        except Exception as exc:
            failed_jobs.append((jid, str(exc)[:160]))
    print(f"\nCOMMITTED — {len(done_jobs)} job(s) cleaned: re-pointed {repointed} "
          f"delivered_records, deleted {deleted} duplicate rows.")
    if failed_jobs:
        print(f"FAILED {len(failed_jobs)} job(s) (rolled back, others committed):")
        for jid, err in failed_jobs:
            print(f"  {jid}: {err}")


if __name__ == "__main__":
    main()
