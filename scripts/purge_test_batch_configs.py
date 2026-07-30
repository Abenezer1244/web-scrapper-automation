"""ONE-TIME, DESTRUCTIVE: hard-delete the 12 test batch-child scraper_configs.

The product has NO hard-delete path — DELETE /scrapers/{id} sets active=False and
says "preserves job history" (src/api/routes/scrapers.py:399). This bypasses that
deliberately, at explicit user request, after the blast radius was measured and
confirmed twice:

    12 scraper_configs -> 12 jobs -> 44,479 results (297 carrying skip-trace PII)
    + 322 job_logs, 12 notifications, 2 user_record_views

DRY-RUN BY DEFAULT. --apply writes, and only after a full backup has been written
and its row counts verified.

    railway run --service worker python scripts/purge_test_batch_configs.py
    railway run --service worker python scripts/purge_test_batch_configs.py --apply --backup <path>

Design (Codex-reviewed):
  * FK map verified against src/db/models.py, not assumed:
      CASCADE  jobs<-configs, results<-jobs, job_logs<-jobs, user_record_views<-configs,
               skip_trace_queues<-jobs, pending_skip_trace_rows<-jobs/results,
               dialer_deliveries<-jobs/results/configs
      SET NULL delivered_records.first_result_id
      SOFT     notifications.job_id, delivered_records.first_job_id,
               batch_runs.child_job_ids / failed_children  <- these DANGLE, so
               notifications are deleted explicitly and the rest are reported.
  * Backup reads via RAW SQL so EncryptedJSON columns come back as CIPHERTEXT —
    no plaintext PII is ever written to disk. Restoring therefore requires the
    CURRENT FIELD_ENCRYPTION_KEY; a key rotation makes the dump undecryptable.
  * One transaction, all-or-nothing. lock_timeout 2s so this can never queue
    behind (or block) a deploy's `alembic upgrade head`; statement_timeout 120s
    because 44k cascaded deletes need real time.
  * Refuses to run if any target config is on a recurring schedule or any batch
    run referencing these jobs is still pending/running.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

# (table, column, id-set) — everything that dies or changes. Verified against models.py.
_CASCADE = [
    ("jobs", "scraper_config_id", "configs"),
    ("user_record_views", "scraper_config_id", "configs"),
    ("results", "job_id", "jobs"),
    ("job_logs", "job_id", "jobs"),
    ("skip_trace_queues", "job_id", "jobs"),
    ("pending_skip_trace_rows", "job_id", "jobs"),
    ("dialer_deliveries", "job_id", "jobs"),
]
_SOFT = [("notifications", "job_id", "jobs")]   # no FK — deleted explicitly


def _ids(db) -> list[str]:
    """The 12 configs, identified from the audit trail the rename wrote."""
    return [
        r.detail.split(":")[0]
        for r in db.execute(
            text(
                "SELECT detail FROM audit_events "
                "WHERE event = 'scraper_config.name_backfilled' ORDER BY created_at"
            )
        ).all()
    ]


def _scalar(db, sql: str, params: dict) -> int:
    return db.execute(text(sql), params).scalar() or 0


def _safety_checks(db, cfg_ids: list[str], job_ids: list[str]) -> None:
    """Refuse on anything that could be actively in flight."""
    recurring = _scalar(
        db,
        "SELECT count(*) FROM scraper_configs WHERE id = ANY(CAST(:ids AS uuid[])) "
        "AND schedule->>'frequency' IS NOT NULL AND schedule->>'frequency' <> 'manual'",
        {"ids": cfg_ids},
    )
    if recurring:
        raise SystemExit(f"ABORT: {recurring} target config(s) are on a recurring schedule.")

    live = _scalar(
        db,
        "SELECT count(*) FROM jobs WHERE scraper_config_id = ANY(CAST(:ids AS uuid[])) "
        "AND status NOT IN ('done','failed','cancelled')",
        {"ids": cfg_ids},
    )
    if live:
        raise SystemExit(f"ABORT: {live} target job(s) are still running.")

    if job_ids:
        pending = _scalar(
            db,
            "SELECT count(*) FROM batch_runs WHERE status IN ('pending','running') "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(child_job_ids::jsonb) e "
            "            WHERE e = ANY(CAST(:jids AS text[])))",
            {"jids": job_ids},
        )
        if pending:
            raise SystemExit(f"ABORT: {pending} batch run(s) referencing these jobs are in flight.")


def _dump(db, cfg_ids, job_ids, path: str) -> dict:
    """Raw-SQL dump of every row about to die. Ciphertext stays ciphertext."""
    sets = {"configs": cfg_ids, "jobs": job_ids}
    payload: dict = {
        "_meta": {
            "written_at": datetime.now(UTC).isoformat(),
            "purpose": "pre-delete backup for scripts/purge_test_batch_configs.py",
            "config_ids": cfg_ids,
            "job_ids": job_ids,
            "WARNING": (
                "results.phones/emails are Fernet CIPHERTEXT. Restoring requires the "
                "FIELD_ENCRYPTION_KEY that was live at write time. Treat as PII."
            ),
        }
    }
    counts: dict = {}
    payload["scraper_configs"] = _rows(db, "scraper_configs", "id", cfg_ids)
    counts["scraper_configs"] = len(payload["scraper_configs"])
    for table, col, which in _CASCADE + _SOFT:
        payload[table] = _rows(db, table, col, sets[which])
        counts[table] = len(payload[table])
    # SET NULL / dangling refs — captured so the damage is reconstructable.
    payload["delivered_records_affected"] = _rows_sql(
        db,
        "SELECT * FROM delivered_records WHERE first_job_id = ANY(CAST(:v AS uuid[])) "
        "OR first_result_id IN (SELECT id FROM results WHERE job_id = ANY(CAST(:v AS uuid[])))",
        {"v": job_ids},
    )
    counts["delivered_records_affected"] = len(payload["delivered_records_affected"])
    payload["batch_runs_referencing"] = _rows_sql(
        db,
        "SELECT * FROM batch_runs WHERE EXISTS ("
        "  SELECT 1 FROM jsonb_array_elements_text(child_job_ids::jsonb) e "
        "  WHERE e = ANY(CAST(:v AS text[])))",
        {"v": job_ids},
    )
    counts["batch_runs_referencing"] = len(payload["batch_runs_referencing"])
    payload["_meta"]["counts"] = counts

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, default=str)
    return counts


def _rows(db, table: str, col: str, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    return _rows_sql(
        db,
        # table/col are literals from _CASCADE/_SOFT above, never input.
        f"SELECT * FROM {table} WHERE {col} = ANY(CAST(:v AS uuid[]))",  # noqa: S608
        {"v": ids},
    )


def _rows_sql(db, sql: str, params: dict) -> list[dict]:
    res = db.execute(text(sql), params)
    cols = list(res.keys())
    return [dict(zip(cols, row, strict=True)) for row in res.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    ap.add_argument("--backup", help="path for the pre-delete JSON dump (required with --apply)")
    args = ap.parse_args()

    if args.apply and not args.backup:
        raise SystemExit("--apply requires --backup <path>. Refusing to delete without one.")

    with system_sync_session() as db:
        cfg_ids = _ids(db)
        if not cfg_ids:
            print("No target configs found (audit trail empty) — nothing to do.")
            return
        job_ids = [
            r.id
            for r in db.execute(
                text(
                    "SELECT id::text AS id FROM jobs "
                    "WHERE scraper_config_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": cfg_ids},
            ).all()
        ]
        _safety_checks(db, cfg_ids, job_ids)

        sets = {"configs": cfg_ids, "jobs": job_ids}
        print(f"=== {len(cfg_ids)} scraper_configs targeted ===\n")
        before = {"scraper_configs": len(cfg_ids)}
        for table, col, which in _CASCADE + _SOFT:
            n = _scalar(
                db,
                f"SELECT count(*) FROM {table} WHERE {col} = ANY(CAST(:v AS uuid[]))",  # noqa: S608
                {"v": sets[which]},
            )
            before[table] = n
            kind = "CASCADE" if (table, col, which) in _CASCADE else "SOFT-REF (explicit delete)"
            print(f"  {table:26} {n:>7}   {kind}")

        dangle_dr = _scalar(
            db,
            "SELECT count(*) FROM delivered_records WHERE first_job_id = ANY(CAST(:v AS uuid[]))",
            {"v": job_ids},
        )
        dangle_br = _scalar(
            db,
            "SELECT count(*) FROM batch_runs WHERE EXISTS ("
            " SELECT 1 FROM jsonb_array_elements_text(child_job_ids::jsonb) e "
            " WHERE e = ANY(CAST(:v AS text[])))",
            {"v": job_ids},
        )
        print(f"\n  delivered_records.first_job_id left dangling : {dangle_dr}")
        print(f"  batch_runs.child_job_ids left dangling       : {dangle_br}")
        print("  scraper_batches parents                      : kept (not deleted)")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply --backup <path>.")
            return

        print(f"\nWriting backup -> {args.backup}")
        counts = _dump(db, cfg_ids, job_ids, args.backup)
        for table, n in before.items():
            got = counts.get(table, 0)
            if got != n:
                raise SystemExit(
                    f"ABORT before delete: backup of {table} has {got} rows, expected {n}."
                )
        print(f"Backup verified: {sum(counts.values())} rows across {len(counts)} tables.")

        # ── the destructive part ────────────────────────────────────────────
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        db.execute(text("SET LOCAL statement_timeout = '120s'"))
        db.execute(text("SET LOCAL idle_in_transaction_session_timeout = '120s'"))
        n_notif = db.execute(
            text("DELETE FROM notifications WHERE job_id = ANY(CAST(:v AS uuid[]))"),
            {"v": job_ids},
        ).rowcount
        n_cfg = db.execute(
            text("DELETE FROM scraper_configs WHERE id = ANY(CAST(:v AS uuid[]))"),
            {"v": cfg_ids},
        ).rowcount
        if n_cfg != len(cfg_ids):
            db.rollback()
            raise SystemExit(
                f"ABORT (rolled back): deleted {n_cfg} configs, expected {len(cfg_ids)}."
            )
        db.execute(
            text(
                "INSERT INTO audit_events (id, event, detail, created_at) "
                "VALUES (CAST(:aid AS uuid), :event, :detail, now())"
            ),
            {
                "aid": str(uuid.uuid4()),
                "event": "scraper_config.purged",
                "detail": (
                    f"hard-deleted {n_cfg} test configs, {before.get('jobs', 0)} jobs, "
                    f"{before.get('results', 0)} results, {n_notif} notifications; "
                    f"backup={os.path.basename(args.backup)}"
                )[:512],
            },
        )
        db.commit()
        print(f"\nDELETED: {n_cfg} configs, {n_notif} notifications (+ cascades).")

        # ── post-delete verification ────────────────────────────────────────
        print("\n=== post-delete verification (all must be 0) ===")
        bad = []
        rem = _scalar(
            db, "SELECT count(*) FROM scraper_configs WHERE id = ANY(CAST(:v AS uuid[]))",
            {"v": cfg_ids},
        )
        print(f"  {'scraper_configs':26} {rem:>7}")
        if rem:
            bad.append("scraper_configs")
        for table, col, which in _CASCADE + _SOFT:
            rem = _scalar(
                db,
                f"SELECT count(*) FROM {table} WHERE {col} = ANY(CAST(:v AS uuid[]))",  # noqa: S608
                {"v": sets[which]},
            )
            print(f"  {table:26} {rem:>7}")
            if rem:
                bad.append(table)
        print("\n" + ("!! RESIDUE IN: " + ", ".join(bad) if bad else "Clean — every target row gone."))


if __name__ == "__main__":
    main()
