"""Deactivate the 12 test batch-child scraper_configs (soft delete).

Same effect as the product's own DELETE /scrapers/{id} (src/api/routes/scrapers.py:399
sets active=False, "preserves job history") — which is also the only deletion the
`bridgeleads_system` role can perform: it has DELETE=False on every table.

Clears these one-shot test configs out of the live view without touching the
44,479 result rows (297 carrying paid skip-trace data) that a hard delete would
cascade away. Fully reversible: flip active back to true.

DRY-RUN BY DEFAULT.

    railway run --service worker python scripts/deactivate_test_batch_configs.py
    railway run --service worker python scripts/deactivate_test_batch_configs.py --apply
"""
import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    args = ap.parse_args()

    with system_sync_session() as db:
        ids = [
            r.detail.split(":")[0]
            for r in db.execute(
                text(
                    "SELECT detail FROM audit_events "
                    "WHERE event = 'scraper_config.name_backfilled' ORDER BY created_at"
                )
            ).all()
        ]
        if not ids:
            print("No target configs found (audit trail empty) — nothing to do.")
            return

        rows = db.execute(
            text(
                "SELECT id::text AS id, name, active FROM scraper_configs "
                "WHERE id = ANY(CAST(:v AS uuid[])) ORDER BY active DESC, name"
            ),
            {"v": ids},
        ).all()
        todo = [r for r in rows if r.active]
        print(f"=== {len(rows)} target configs, {len(todo)} still active ===\n")
        for r in rows:
            print(f"  [{'ACTIVE ' if r.active else 'already off'}] {r.name}")

        if not todo:
            print("\nAll already inactive — nothing to do.")
            return
        if not args.apply:
            print(f"\nDRY RUN — would deactivate {len(todo)}. Re-run with --apply.")
            return

        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        db.execute(text("SET LOCAL statement_timeout = '15s'"))
        # Compare-and-swap on active=true: a config someone re-enabled meanwhile
        # simply doesn't match, and the count check below catches it.
        n = db.execute(
            text(
                "UPDATE scraper_configs SET active = false, updated_at = now() "
                "WHERE id = ANY(CAST(:v AS uuid[])) AND active = true"
            ),
            {"v": [r.id for r in todo]},
        ).rowcount
        if n != len(todo):
            db.rollback()
            raise SystemExit(
                f"ABORT (rolled back): updated {n}, expected {len(todo)} — a config changed."
            )
        db.execute(
            text(
                "INSERT INTO audit_events (id, event, detail, created_at) "
                "VALUES (CAST(:aid AS uuid), :event, :detail, now())"
            ),
            {
                "aid": str(uuid.uuid4()),
                "event": "scraper_config.deactivated_bulk",
                "detail": f"soft-deleted {n} one-shot test batch configs (reversible)"[:512],
            },
        )
        db.commit()
        print(f"\nDEACTIVATED: {n} configs (reversible — set active=true to undo).")

        left = db.execute(
            text(
                "SELECT count(*) FROM scraper_configs "
                "WHERE id = ANY(CAST(:v AS uuid[])) AND active = true"
            ),
            {"v": ids},
        ).scalar()
        print(f"verification: {left} of the 12 still active (expect 0)")


if __name__ == "__main__":
    main()
