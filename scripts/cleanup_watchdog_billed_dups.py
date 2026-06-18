"""Billing-aware cleanup of watchdog-appended BILLED duplicate result rows (2026-06-17).

This is the DEFERRED sibling of ``cleanup_watchdog_dup_results.py``. That script
removes the watchdog-appended copies that the re-run's dedup already marked
``is_duplicate=true`` (zero billing impact) and REFUSES to touch any
``is_duplicate=false`` row. A subset of watchdog jobs, however, appended copies
that were NOT marked duplicate (the re-run's dedup did not claim them) — so the
job was billed MORE THAN ONCE for the same scraped record. Those over-billed
``is_duplicate=false`` rows are what THIS script removes, and it ALSO reverses the
over-charge on ``users.records_used`` where that is still the right thing to do.

It is deliberately a SEPARATE script (not a flag on the safe one): the safe
script's "refuse to delete any non-duplicate row" guard is load-bearing for the
already-completed safe subset, and must not be conditionally weakened.

WHAT IT DOES (per --ids job, one ATOMIC transaction per job)
------------------------------------------------------------
1. Group the job's rows by SCRAPE-TIME content identity (same fingerprint as the
   safe script — enrichment fields excluded, so original + watchdog copies group
   together even after divergent enrichment; genuinely-distinct leads differ in a
   scrape field and so are NEVER grouped/removed).
2. Pick ONE survivor per group; the survivor is is_duplicate=false whenever any
   group member is (the ranking sorts is_duplicate ASC first), so the kept row is
   the billable one. Re-point any ``delivered_records.first_result_id`` that
   referenced a doomed row to the survivor (FK is ON DELETE SET NULL).
3. Delete the non-survivors.
4. Decrement ``users.records_used`` by the number of DELETED ``is_duplicate=false``
   rows for the job — BUT ONLY when the charge is still reflected in the current
   billing period (see PERIOD-AWARE RULE). No GREATEST/clamp: the decrement uses
   ``WHERE records_used >= :dec`` and asserts exactly one row changed, so a counter
   that can't absorb the decrement fails LOUD and rolls the whole job back.
5. If the deploy that adds ``jobs.billed_count`` has landed, recompute it from the
   surviving non-duplicate rows.

PERIOD-AWARE RULE (the monthly-reset trap — the most important safety property)
-------------------------------------------------------------------------------
The daily reset task advances ``users.records_period_start`` to the first of the
month and resets ``records_used``. So a charge applied in a PRIOR period has
ALREADY been wiped from the current ``records_used`` — decrementing for it now
would wrongly subtract from THIS month's usage. Therefore:

    effective_billed_at = COALESCE(jobs.billing_applied_at, jobs.finished_at)
    decrement ONLY when effective_billed_at >= users.records_period_start
    older (or null period_start) -> DELETE-ONLY, no decrement
    effective_billed_at IS NULL  -> REFUSE the job (can't reason about billing)

(``jobs.billing_applied_at`` only exists once migration 063 / PR #59 is deployed;
before that this script transparently falls back to ``finished_at``.)

IDEMPOTENCY
-----------
The unit of work is the exact set of doomed result IDs. After a successful commit
those rows are gone, so a re-run with the same --ids recomputes an empty plan:
nothing to delete, nothing to decrement. A crash mid-job rolls that job back
whole (single transaction), so re-running is always safe.

GRANTS
------
``--commit`` DELETEs from results, UPDATEs delivered_records, and UPDATEs users —
none of which the worker role (bridgeleads_system) can do. Provide an OWNER/admin
sync DSN via the ``ADMIN_DATABASE_URL_SYNC`` env var (never argv — a DSN in argv
leaks via process listings). DRY-RUN uses the normal system session (reads only).

USAGE
-----
    # dry-run (read-only) — REQUIRED explicit job ids, prints the per-job plan:
    railway run --service worker python scripts/cleanup_watchdog_billed_dups.py --ids <id> [<id> ...]

    # APPLY (owner DSN + BOTH safety flags required):
    ADMIN_DATABASE_URL_SYNC=... python scripts/cleanup_watchdog_billed_dups.py \
        --ids <id> [<id> ...] --commit --i-understand-billing-decrement

Run with DEBUG unset/false and WITHOUT piping stdout through a pager/grep: the
safe-subset run stalled `idle in transaction` once due to stdout backpressure.
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

# Scrape-stable content identity — IDENTICAL to cleanup_watchdog_dup_results.py.
# Excludes property/mailing/enrichment (they drift on enrichment); raw_html_hash is
# COMBINED WITH the tuple (never chosen instead — a page/batch-level hash chosen alone
# could collapse distinct leads; as one more discriminator it can only INCREASE
# distinctness). jsonb_build_array = NULL- and delimiter-safe encoding.
_CFP_SQL = (
    "md5(jsonb_build_array("
    "nullif(r.raw_html_hash, ''), r.parcel_id, r.party_name, "
    "r.date_recorded::text, r.doc_type, r.legal_description, r.dedup_hash, "
    "r.delinquent_amount::text, r.delinquent_bill_year::text"
    ")::text)"
)

# Per (job, content) survivor ranking; rn=1 kept, rn>1 deleted. NOTE the difference
# from the safe script: is_duplicate ASC ranks FIRST here, so the survivor is always
# is_duplicate=false when the group has any false row — the kept row is the billable
# one and stays billable (locked decision: survivor stays is_duplicate=false if any
# member is). Anchors that point at a doomed row are re-pointed to the survivor.
# _CFP_SQL is a hardcoded module constant (no user input); :job_ids is bound.
_RANK_SQL = f"""
    SELECT r.id::text AS id, r.job_id::text AS job_id, r.is_duplicate,
           (dr.first_result_id IS NOT NULL) AS is_anchor,
           {_CFP_SQL} AS cfp,
           row_number() OVER (
               PARTITION BY r.job_id, {_CFP_SQL}
               ORDER BY r.is_duplicate ASC,
                        (dr.first_result_id IS NOT NULL) DESC,
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


def _column_exists(db, table: str, column: str) -> bool:
    """True if a column is present in the CURRENT schema (pre-deploy prod lacks
    billing_applied_at/billed_count). Schema-qualified so a same-named table in
    another schema can't produce a false positive (Codex)."""
    return bool(
        db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _job_billing_meta(db, job_ids: list[str], has_billing_applied: bool,
                      *, lock: bool = False) -> dict:
    """Per-job billing context computed IN SQL (timezone-safe comparison).

    Returns {job_id: {status, user_id, effective_billed_at, records_used,
                      records_period_start, period_current}}.
    effective_billed_at = COALESCE(billing_applied_at?, finished_at).
    period_current = effective_billed_at >= records_period_start (both non-null).

    ``lock=True`` adds ``FOR UPDATE OF j, u`` — used inside the apply transaction so
    the daily reset task cannot advance records_period_start / reset records_used
    between this read and the decrement (Codex Critical: the billing decision MUST be
    made from fresh, locked state, never from the dry-run snapshot).
    """
    eff = "COALESCE(j.billing_applied_at, j.finished_at)" if has_billing_applied else "j.finished_at"
    lock_clause = " FOR UPDATE OF j, u" if lock else ""
    rows = db.execute(
        text(
            f"""
            SELECT j.id::text AS id, j.status, j.user_id::text AS user_id,
                   {eff} AS effective_billed_at,
                   u.records_used AS records_used,
                   u.records_period_start AS records_period_start,
                   ({eff} IS NOT NULL AND u.records_period_start IS NOT NULL
                    AND {eff} >= u.records_period_start) AS period_current
            FROM jobs j JOIN users u ON u.id = j.user_id
            WHERE j.id = ANY(CAST(:job_ids AS uuid[])){lock_clause}
            """  # noqa: S608 — eff/lock_clause are hardcoded literals; :job_ids is bound
        ),
        {"job_ids": job_ids},
    ).fetchall()
    return {
        row.id: {
            "status": row.status,
            "user_id": row.user_id,
            "effective_billed_at": row.effective_billed_at,
            "records_used": row.records_used,
            "records_period_start": row.records_period_start,
            "period_current": bool(row.period_current),
        }
        for row in rows
    }


def _sample_multibilled_groups(db, job_ids: list[str], limit: int = 8) -> list:
    """For human eyeball before --commit (Codex over-group guard): a few fingerprint
    groups that contain MORE THAN ONE is_duplicate=false row — i.e. the rows whose
    deletion would reverse a charge. If any of these are genuinely-distinct leads that
    merely share every scrape field, this is where it shows up."""
    return db.execute(
        text(
            f"""
            WITH g AS (
                SELECT r.job_id::text AS job_id, {_CFP_SQL} AS cfp,
                       count(*) FILTER (WHERE r.is_duplicate = false) AS billed,
                       count(*) AS total,
                       min(r.parcel_id) AS parcel, min(r.party_name) AS party,
                       min(r.doc_type) AS doc, min(r.date_recorded::text) AS dt
                FROM results r
                WHERE r.job_id = ANY(CAST(:job_ids AS uuid[]))
                GROUP BY r.job_id, {_CFP_SQL}
            )
            SELECT job_id, billed, total, parcel, party, doc, dt
            FROM g WHERE billed > 1
            ORDER BY billed DESC
            LIMIT :lim
            """  # noqa: S608 — _CFP_SQL is a constant; :job_ids/:lim are bound
        ),
        {"job_ids": job_ids, "lim": limit},
    ).fetchall()


def _plan(db, job_ids: list[str]) -> dict:
    """Read-only deletion plan per job: survivor per group, doomed ids, the count of
    DELETED is_duplicate=false rows (the decrement amount), and the doomed-anchor ->
    survivor re-point map."""
    ranked = db.execute(text(_RANK_SQL), {"job_ids": job_ids}).fetchall()
    survivor: dict[tuple[str, str], str] = {}
    for row in ranked:
        if row.rn == 1:
            survivor[(row.job_id, row.cfp)] = row.id
    by_job: dict[str, dict] = {}
    for row in ranked:
        j = by_job.setdefault(
            row.job_id,
            {"total": 0, "keep": 0, "delete": 0, "delete_ids": [],
             "delete_billed": 0, "repoint": []},
        )
        j["total"] += 1
        if row.rn == 1:
            j["keep"] += 1
            continue
        j["delete"] += 1
        j["delete_ids"].append(row.id)
        if not row.is_duplicate:
            # An originally-billed copy of an already-counted record. Its deletion
            # is what the records_used decrement reverses.
            j["delete_billed"] += 1
        if row.is_anchor:
            j["repoint"].append((row.id, survivor[(row.job_id, row.cfp)]))
    return by_job


def _chunks(values: list[str], size: int = BATCH_SIZE):
    for k in range(0, len(values), size):
        yield values[k:k + size]


def _assert_terminal(db, job_ids: list[str]) -> None:
    rows = db.execute(
        text("SELECT id::text AS id, status FROM jobs WHERE id = ANY(CAST(:j AS uuid[]))"),
        {"j": job_ids},
    ).fetchall()
    found = {row.id for row in rows}
    missing = sorted(set(job_ids) - found)
    non_terminal = [f"{row.id}:{row.status}" for row in rows
                    if row.status not in TERMINAL_JOB_STATUSES]
    if missing or non_terminal:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if non_terminal:
            details.append(f"non_terminal={non_terminal}")
        raise RuntimeError("refusing cleanup for unsafe job scope: " + "; ".join(details))


def _assert_no_delivered_anchors(db, ids: list[str], context: str) -> None:
    still = db.execute(
        text("SELECT count(*) FROM delivered_records "
             "WHERE first_result_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": ids},
    ).scalar()
    if still:
        raise RuntimeError(f"{still} delivered_records still point at to-delete rows ({context})")


BACKUP_TABLE = "results_watchdog_billed_backup"
# delivered_records.first_result_id is ON DELETE SET NULL and is handled explicitly
# (re-pointed to the survivor); every OTHER FK referencing results(id) must have ZERO
# references to the doomed set or the delete is unsafe. Discovered at runtime, not
# hardcoded, so a future FK can't silently slip past (Codex).
_HANDLED_FK = ("delivered_records", "first_result_id")


def _referencing_fks(db) -> list[tuple[str, str, int, str]]:
    """Every FK referencing the results table: (table, column, n_cols, referenced_col).
    Runtime catalog scan so the safety check is self-proving against schema drift, not a
    static list. n_cols/referenced_col let the caller FAIL CLOSED on unexpected shapes
    (composite FKs, or an FK to a non-id results column) rather than silently mis-count."""
    rows = db.execute(
        text(
            "SELECT con.conrelid::regclass::text AS tbl, att.attname AS col, "
            "       cardinality(con.conkey) AS ncols, refatt.attname AS refcol "
            "FROM pg_constraint con "
            "JOIN pg_attribute att ON att.attrelid = con.conrelid "
            "                     AND att.attnum = con.conkey[1] "
            "LEFT JOIN pg_attribute refatt ON refatt.attrelid = con.confrelid "
            "                             AND refatt.attnum = con.confkey[1] "
            "WHERE con.contype = 'f' AND con.confrelid = 'results'::regclass"
        )
    ).fetchall()
    return [(r.tbl, r.col, r.ncols, r.refcol) for r in rows]


def _id_referencing_fks(db) -> list[tuple[str, str]]:
    """The single-column FKs that reference results(id), as (table, column). FAILS CLOSED
    on any unexpected shape — a composite FK or one referencing a non-id results column
    means manual review, never a silent skip."""
    out: list[tuple[str, str]] = []
    for tbl, col, ncols, refcol in _referencing_fks(db):
        if ncols != 1 or refcol != "id":
            raise RuntimeError(
                f"unexpected FK shape {tbl}.{col} -> results({refcol}) ncols={ncols}; "
                "manual review required before deletion")
        out.append((tbl, col))
    return out


def _count_fk_refs(db, table: str, col: str, ids: list[str]) -> int:
    return db.execute(
        text(f"SELECT count(*) FROM {table} WHERE {col} = ANY(CAST(:ids AS uuid[]))"),  # noqa: S608 — table/col from pg_catalog, not user input
        {"ids": ids},
    ).scalar()


def _assert_no_other_refs(db, ids: list[str], context: str) -> None:
    """No table other than the explicitly-handled delivered_records.first_result_id may
    reference the doomed rows. Covers CASCADE (would cascade-delete a skip-trace queue /
    dialer outbox row) AND SET NULL / RESTRICT (would orphan or block) uniformly, and
    fails closed on unexpected FK shapes. Runs inside the txn so a reference acquired
    since the dry-run aborts the job."""
    for tbl, col in _id_referencing_fks(db):
        if (tbl, col) == _HANDLED_FK:
            continue
        n = _count_fk_refs(db, tbl, col, ids)
        if n:
            raise RuntimeError(
                f"{n} {tbl}.{col} rows reference to-delete results ({context}) — unsafe; refusing")


def _ensure_backup_table(db) -> None:
    """Row-level archive for rollback (Codex): every deleted row is preserved as JSONB
    (version-proof) before deletion, in the SAME transaction as the delete. Rollback =
    re-insert from row_data. Created once; IF NOT EXISTS so re-runs are safe.

    The archive holds full result rows (plaintext names/addresses + encrypted PII
    ciphertext), so it is locked down on creation (Codex High): REVOKE ALL FROM PUBLIC
    and ENABLE ROW LEVEL SECURITY with no policy — the owner (running this) bypasses
    RLS for rollback, every other role is default-denied and PostgREST can't expose it."""
    db.execute(text(
        f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} ("
        "  id uuid NOT NULL,"
        "  job_id uuid,"
        "  run_label text,"
        "  backed_up_at timestamptz NOT NULL DEFAULT now(),"
        "  row_data jsonb NOT NULL,"
        f"  CONSTRAINT pk_{BACKUP_TABLE} PRIMARY KEY (id, backed_up_at)"
        ")"
    ))
    db.execute(text(f"REVOKE ALL ON {BACKUP_TABLE} FROM PUBLIC"))
    # REVOKE FROM PUBLIC does NOT remove DIRECT role grants, and a BYPASSRLS role
    # (e.g. Supabase service_role) would still read through RLS — so revoke the
    # privilege itself from every app/API role that might exist (Codex). No privilege
    # = no read, regardless of BYPASSRLS. Guarded so a missing role isn't an error.
    revoke_roles_sql = (
        "DO $$ DECLARE r text; BEGIN "  # noqa: S608 — BACKUP_TABLE is a module constant; format() %I quotes the role
        "  FOREACH r IN ARRAY ARRAY['anon','authenticated','service_role',"
        "                           'bridgeleads_app','bridgeleads_system'] LOOP "
        "    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN "
        f"      EXECUTE format('REVOKE ALL ON {BACKUP_TABLE} FROM %I', r); "
        "    END IF; "
        "  END LOOP; "
        "END $$;"
    )
    db.execute(text(revoke_roles_sql))
    db.execute(text(f"ALTER TABLE {BACKUP_TABLE} ENABLE ROW LEVEL SECURITY"))
    # FORCE so even the table OWNER is subject to RLS (a plain owner otherwise bypasses
    # it) — closes the residual hole if this table is ever owned by a non-exposed app
    # role. The admin/owner role that runs --commit has the BYPASSRLS attribute, which
    # overrides FORCE, so rollback reads still work (verified: postgres rolbypassrls=t).
    db.execute(text(f"ALTER TABLE {BACKUP_TABLE} FORCE ROW LEVEL SECURITY"))
    db.commit()


def _print_dryrun(plan: dict, meta: dict, has_billed_count: bool) -> tuple[int, int, int, list[str]]:
    """Print the per-job plan; return (total_delete, total_decrement, total_repoint, refused)."""
    total_del = total_dec = total_anchor = 0
    refused: list[str] = []
    for jid, j in sorted(plan.items(), key=lambda kv: -kv[1]["delete"]):
        if j["delete"] == 0:
            continue
        m = meta.get(jid, {})
        eff = m.get("effective_billed_at")
        period_current = m.get("period_current", False)
        if eff is None:
            # No reliable billing timestamp -> we cannot reason about the charge. Refuse.
            refused.append(jid)
            print(f"  {jid} total={j['total']} keep={j['keep']} delete={j['delete']} "
                  f"delete_billed={j['delete_billed']}  !REFUSE: no billing/finished timestamp")
            continue
        decrement = j["delete_billed"] if period_current else 0
        total_del += j["delete"]
        total_dec += decrement
        total_anchor += len(j["repoint"])
        mode = "DECREMENT" if period_current else "DELETE-ONLY (prior period)"
        ru = m.get("records_used")
        warn = ""
        if period_current and ru is not None and ru < j["delete_billed"]:
            warn = "  !records_used<delete_billed (will FAIL the >= guard)"
        print(f"  {jid} total={j['total']} keep={j['keep']} delete={j['delete']} "
              f"delete_billed={j['delete_billed']} -> {mode} decrement={decrement} "
              f"repoint={len(j['repoint'])}{warn}")
    print(f"\nTOTAL would delete {total_del} rows, decrement records_used by {total_dec}, "
          f"re-point {total_anchor} delivered_records.")
    if not has_billed_count:
        print("NOTE: jobs.billed_count column absent (pre-deploy) - billed_count recompute skipped.")
    if refused:
        print(f"REFUSED {len(refused)} job(s) with no billing/finished timestamp: {refused}")
    return total_del, total_dec, total_anchor, refused


def _apply_job(AdminSession, jid: str, j: dict, has_billing_applied: bool,  # noqa: N803 — sessionmaker factory is legitimately PascalCase
               has_billed_count: bool, run_label: str) -> dict:
    """Apply ONE job in a SINGLE atomic transaction: lock+re-read billing state,
    repoint anchors, delete doomed, decrement records_used (period-gated, fail-loud),
    recompute billed_count. The dry-run snapshot (``j``) supplies only the EXPECTED
    delete-id set for a drift check; EVERY billing decision is recomputed here from
    fresh, locked state (Codex Critical). Raises on any inconsistency -> whole job
    rolls back -> a re-run with the same --ids is a no-op (the rows are already gone)."""
    expected_ids = set(j["delete_ids"])

    with AdminSession() as adb:
        _assert_terminal(adb, [jid])

        # Fresh, LOCKED billing metadata — records_period_start / records_used cannot
        # shift under us between this read and the decrement below.
        m = _job_billing_meta(adb, [jid], has_billing_applied, lock=True).get(jid)
        if m is None:
            raise RuntimeError(f"job {jid} or its user row missing — refusing")
        if m["effective_billed_at"] is None:
            raise RuntimeError(f"job {jid}: no reliable billing/finished timestamp — refusing")

        # Fresh deletion plan (admin role, same transaction). Drift since dry-run is fatal.
        fresh = _plan(adb, [jid]).get(jid, {"delete_ids": [], "delete_billed": 0, "repoint": []})
        ids = fresh["delete_ids"]
        if set(ids) != expected_ids:
            raise RuntimeError(
                f"plan drifted since dry-run (planned {len(expected_ids)} deletes, now "
                f"{len(ids)}) — re-run dry-run")

        # Decrement is computed from FRESH plan + FRESH locked period flag, never the snapshot.
        decrement = fresh["delete_billed"] if m["period_current"] else 0

        # 1. Re-point delivered_records anchors to the surviving row. Assert every
        #    survivor row actually exists first (belt-and-suspenders over the FK).
        survivors = sorted({s for _, s in fresh["repoint"]})
        if survivors:
            present = adb.execute(
                text("SELECT count(*) FROM results WHERE id = ANY(CAST(:s AS uuid[]))"),
                {"s": survivors},
            ).scalar()
            if present != len(survivors):
                raise RuntimeError(
                    f"job {jid}: {len(survivors) - present} survivor row(s) missing — refusing")
        repointed = 0
        for doomed_id, survivor_id in fresh["repoint"]:
            repointed += adb.execute(
                text("UPDATE delivered_records SET first_result_id = CAST(:s AS uuid) "
                     "WHERE first_result_id = CAST(:d AS uuid)"),
                {"s": survivor_id, "d": doomed_id},
            ).rowcount
        _assert_no_delivered_anchors(adb, ids, f"job {jid} after re-point")
        # No table other than the handled delivered_records may reference a doomed row
        # (a CASCADE FK would cascade-delete a skip-trace/dialer record; SET NULL/RESTRICT
        # would orphan/block). Catalog-discovered, re-asserted here inside the txn.
        _assert_no_other_refs(adb, ids, f"job {jid}")

        # 2. Archive then delete, in sub-batched statements (ONE transaction). The
        #    backup INSERT and the DELETE share the job's transaction, so a row is
        #    never deleted without first being preserved (rollback = re-insert row_data).
        deleted = 0
        for chunk in _chunks(ids):
            backed = adb.execute(
                text(f"INSERT INTO {BACKUP_TABLE} (id, job_id, run_label, row_data) "  # noqa: S608 — BACKUP_TABLE is a constant
                     "SELECT r.id, r.job_id, :label, to_jsonb(r) FROM results r "
                     "WHERE r.id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": chunk, "label": run_label},
            ).rowcount
            if backed != len(chunk):
                raise RuntimeError(f"backed up {backed} != batch {len(chunk)} for job {jid}")
            batch = adb.execute(
                text("DELETE FROM results WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": chunk},
            ).rowcount
            if batch != len(chunk):
                raise RuntimeError(f"deleted {batch} != batch {len(chunk)} for job {jid}")
            deleted += batch
        if deleted != len(ids):
            raise RuntimeError(f"deleted {deleted} != planned {len(ids)} for job {jid}")

        # 3. Reverse the over-charge — ONLY for current-period charges. WHERE >= guard
        #    + rowcount assertion instead of GREATEST: a counter that can't absorb the
        #    decrement fails loud and rolls the whole job back (Codex locked decision).
        if decrement:
            moved = adb.execute(
                text("UPDATE users SET records_used = records_used - :dec "
                     "WHERE id = CAST(:uid AS uuid) AND records_used >= :dec"),
                {"dec": decrement, "uid": m["user_id"]},
            ).rowcount
            if moved != 1:
                raise RuntimeError(
                    f"records_used decrement of {decrement} for user {m['user_id']} "
                    f"affected {moved} rows (records_used < {decrement}?) — refusing job {jid}")

        # 4. Recompute jobs.billed_count from surviving non-dup rows (if column exists).
        if has_billed_count:
            adb.execute(
                text("UPDATE jobs SET billed_count = ("
                     "  SELECT count(*) FROM results "
                     "  WHERE job_id = CAST(:jid AS uuid) AND is_duplicate = false"
                     ") WHERE id = CAST(:jid AS uuid)"),
                {"jid": jid},
            )

        adb.commit()
    return {"deleted": deleted, "repointed": repointed, "decremented": decrement}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ids", nargs="+", required=True, help="explicit job ids (REQUIRED)")
    ap.add_argument("--commit", action="store_true", help="apply (needs owner DSN + confirm flag)")
    ap.add_argument("--i-understand-billing-decrement", dest="confirm_billing",
                    action="store_true",
                    help="required with --commit: this DELETES billed rows and decrements records_used")
    ap.add_argument("--run-label", default="watchdog_billed_cleanup",
                    help="label stored on every archived (deleted) row for rollback/audit")
    args = ap.parse_args()
    job_ids = args.ids
    admin_dsn = os.getenv("ADMIN_DATABASE_URL_SYNC")  # env-only; never argv

    with system_sync_session() as db:
        has_billing_applied = _column_exists(db, "jobs", "billing_applied_at")
        has_billed_count = _column_exists(db, "jobs", "billed_count")
        print(f"Scope: {len(job_ids)} job(s). "
              f"billing_applied_at column={'present' if has_billing_applied else 'ABSENT (pre-deploy)'}, "
              f"billed_count column={'present' if has_billed_count else 'ABSENT'}.")
        plan = _plan(db, job_ids)
        meta = _job_billing_meta(db, job_ids, has_billing_applied)
        samples = _sample_multibilled_groups(db, job_ids)
        # Surface references to the doomed set from EVERY FK on results(id) except the
        # handled delivered_records (catalog-discovered, so drift-proof).
        all_delete_ids = [rid for jp in plan.values() for rid in jp["delete_ids"]] \
            or ["00000000-0000-0000-0000-000000000000"]
        ref_counts = {
            f"{tbl}.{col}": _count_fk_refs(db, tbl, col, all_delete_ids)
            for tbl, col in _id_referencing_fks(db)
            if (tbl, col) != _HANDLED_FK
        }

    _print_dryrun(plan, meta, has_billed_count)

    if samples:
        print("\nSAMPLE multi-billed fingerprint groups (eyeball these are NOT distinct leads):")
        for s in samples:
            print(f"  job={s.job_id[:8]} billed={s.billed} total={s.total} "
                  f"parcel={s.parcel} party={(s.party or '')[:24]!r} doc={s.doc} date={s.dt}")
    refs_str = ", ".join(f"{k}={v}" for k, v in ref_counts.items()) or "(none)"
    print(f"\nFK references to to-delete rows (excl. handled delivered_records): "
          f"{refs_str}  (all must be 0 — else apply aborts).")

    if not args.commit:
        print("\nDRY-RUN -- no rows deleted, no billing changed. "
              "Re-run with --commit --i-understand-billing-decrement + owner DSN to apply.")
        return

    # ── --commit guards ──────────────────────────────────────────────────────
    # NOTE: the dry-run `refused` list (above) is for display only. The authoritative
    # commit-time refusal is recomputed on the ADMIN connection below, so a column-
    # detection difference between the two roles can't cause a false refusal (Codex).
    if not args.confirm_billing:
        print("\nREFUSING --commit: this pass DELETES billed rows and DECREMENTS records_used. "
              "Re-run with --i-understand-billing-decrement to confirm you understand.")
        return
    if not admin_dsn:
        print("\nREFUSING --commit: set ADMIN_DATABASE_URL_SYNC (owner/admin DSN). The worker "
              "role lacks DELETE on results + UPDATE on delivered_records/users.")
        return

    admin_engine = create_engine(admin_dsn, pool_pre_ping=True)
    AdminSession = sessionmaker(admin_engine)

    # Re-detect column presence AND recompute the billing metadata on the ADMIN
    # connection (Codex High + Medium): the system and admin DSNs are different roles
    # and could resolve a different schema/search_path. _apply_job generates SQL for —
    # and refuses based on — the DB it actually writes to.
    with AdminSession() as adb:
        adm_has_billing_applied = _column_exists(adb, "jobs", "billing_applied_at")
        adm_has_billed_count = _column_exists(adb, "jobs", "billed_count")
        adm_meta = _job_billing_meta(adb, job_ids, adm_has_billing_applied)
    if (adm_has_billing_applied, adm_has_billed_count) != (has_billing_applied, has_billed_count):
        print(f"\nNOTE: admin connection sees different columns "
              f"(billing_applied_at={adm_has_billing_applied}, billed_count={adm_has_billed_count}) "
              "than the system connection — using the admin view for writes.")

    # Authoritative refusal gate: any job with deletions but no reliable billing
    # timestamp (missing row, or both billing_applied_at and finished_at NULL).
    adm_refused = [
        jid for jid, jp in plan.items()
        if jp["delete"] and (adm_meta.get(jid) is None
                             or adm_meta[jid]["effective_billed_at"] is None)
    ]
    if adm_refused:
        print(f"\nREFUSING --commit: {len(adm_refused)} job(s) have no reliable billing "
              f"timestamp {adm_refused}. Remove them from --ids and re-run.")
        return

    # Create the row-level archive table once (its own txn) before any deletes.
    with AdminSession() as adb:
        _ensure_backup_table(adb)
    print(f"Archiving deleted rows to {BACKUP_TABLE} (run_label={args.run_label!r}).")

    done: list[tuple[str, dict]] = []
    failed: list[tuple[str, str]] = []
    for jid, j in plan.items():
        if not j["delete_ids"]:
            continue
        try:
            res = _apply_job(AdminSession, jid, j, adm_has_billing_applied,
                             adm_has_billed_count, args.run_label)
            done.append((jid, res))
        except Exception as exc:  # one job's failure rolls back ONLY that job
            failed.append((jid, str(exc)[:200]))

    total_deleted = sum(r["deleted"] for _, r in done)
    total_decremented = sum(r["decremented"] for _, r in done)
    total_repointed = sum(r["repointed"] for _, r in done)
    print(f"\nCOMMITTED -- {len(done)} job(s): deleted {total_deleted} billed-dup rows, "
          f"decremented records_used by {total_decremented}, re-pointed {total_repointed} "
          f"delivered_records.")
    if failed:
        print(f"FAILED {len(failed)} job(s) (each rolled back whole, others committed):")
        for jid, err in failed:
            print(f"  {jid}: {err}")


if __name__ == "__main__":
    main()
