"""Rename batch-child scraper_configs whose display name collides.

Batch children used to be named "{County} {record_type} (batch)", discarding the
parent batch's user-supplied name (fixed forward in src/api/routes/batches.py).
Rows created before that fix still collide, which leaves the dashboard Scrapers
table unable to tell them apart. This rebuilds those names with the SAME
derive_batch_child_name() the live code uses, so old and new rows agree.

DRY-RUN BY DEFAULT — prints `id: old -> new` and exits without writing. Pass
--apply to commit.

    railway run --service worker python scripts/backfill_batch_child_names.py
    railway run --service worker python scripts/backfill_batch_child_names.py --apply

Scope: ONLY configs whose name is duplicated within their own user's list, and
only where the parent batch has a usable name. Everything else is left alone —
the point is to remove ambiguity, not to relabel the account.

Safety (Codex-reviewed):
  * plan is computed and validated BEFORE any write; the read session is closed
    first so nothing is held open across the diagnostics
  * refuses to write unless every rebuilt name is unique within its user
  * compare-and-swap on (id, name, batch_id) — a config edited since the plan was
    computed simply doesn't match, and a rowcount mismatch rolls the whole thing
    back rather than writing a half-applied rename
  * SET LOCAL lock_timeout/statement_timeout so this can never sit on a row lock
    while a deploy runs `alembic upgrade head` (see incident: a long backfill's
    ACCESS SHARE blocked an ALTER and 502'd the API)
  * writes an audit_events row per rename — a system-session UPDATE bypasses the
    API's audit path, and a silent rename of user-visible labels is not okay
"""
import argparse
import os
import sys
import uuid
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.api.routes.batches import derive_batch_child_name  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402


def build_plan() -> list[dict]:
    """Read-only: return [{id, user_id, old, new, batch_id}] for colliding children."""
    with system_sync_session() as db:
        rows = db.execute(
            text(
                "SELECT sc.id::text AS id, sc.user_id::text AS user_id, sc.name, "
                "sc.county, sc.record_type, sc.batch_id::text AS batch_id, "
                "sb.name AS batch_name "
                "FROM scraper_configs sc "
                "LEFT JOIN scraper_batches sb ON sb.id = sc.batch_id"
            )
        ).all()

    by_user_name = defaultdict(list)
    for r in rows:
        by_user_name[(r.user_id, (r.name or "").strip().lower())].append(r)

    plan: list[dict] = []
    skipped: list[str] = []
    for group in by_user_name.values():
        if len(group) < 2:
            continue                      # unique already — nothing to disambiguate
        for c in group:
            if c.batch_id is None or not (c.batch_name or "").strip():
                # Not a batch child, or the parent has no name to rebuild from.
                # There is nothing better to derive, so leave it: the dashboard's
                # created-at hint still disambiguates it.
                skipped.append(f"{c.id[:8]} ({c.name!r}) — no usable parent batch name")
                continue
            new = derive_batch_child_name(c.batch_name, c.county, c.record_type)
            if new == c.name:
                continue                  # already correct — keeps this idempotent
            plan.append(
                {"id": c.id, "user_id": c.user_id, "old": c.name,
                 "new": new, "batch_id": c.batch_id}
            )

    for s in skipped:
        print(f"  SKIP {s}")
    return plan


def validate(plan: list[dict]) -> None:
    """Refuse to write anything unless the whole plan is coherent."""
    if not plan:
        return
    # Every rebuilt name must be unique within its user, or we'd just be swapping
    # one collision for another.
    per_user = defaultdict(list)
    for p in plan:
        per_user[p["user_id"]].append(p["new"].strip().lower())
    for uid, names in per_user.items():
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise SystemExit(
                f"ABORT: rebuilt names still collide for user {uid}: {sorted(dupes)}"
            )
    if len({p["id"] for p in plan}) != len(plan):
        raise SystemExit("ABORT: the same config appears twice in the plan")


def apply(plan: list[dict]) -> None:
    """One short, guarded transaction. All-or-nothing."""
    with system_sync_session() as db:
        # Never queue behind a lock: fail fast instead of blocking a concurrent
        # migration's ACCESS EXCLUSIVE.
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        updated = 0
        for p in plan:
            # Compare-and-swap: if someone renamed or re-parented this config
            # since the plan was built, this matches 0 rows and we bail below.
            res = db.execute(
                text(
                    "UPDATE scraper_configs SET name = :new, updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND name = :old "
                    "AND batch_id = CAST(:batch_id AS uuid)"
                ),
                {"new": p["new"], "id": p["id"], "old": p["old"], "batch_id": p["batch_id"]},
            )
            updated += res.rowcount
            # A system-session UPDATE bypasses the API's audit path, so record it
            # here. No PII by convention — ids and the label only.
            db.execute(
                text(
                    "INSERT INTO audit_events (id, event, user_id, detail, created_at) "
                    "VALUES (CAST(:aid AS uuid), :event, CAST(:uid AS uuid), :detail, now())"
                ),
                {
                    "aid": str(uuid.uuid4()),
                    "event": "scraper_config.name_backfilled",
                    "uid": p["user_id"],
                    "detail": f"{p['id']}: {p['old']!r} -> {p['new']!r}"[:512],
                },
            )
        if updated != len(plan):
            db.rollback()
            raise SystemExit(
                f"ABORT (rolled back): expected to update {len(plan)} rows, matched "
                f"{updated}. A config changed since the plan was built — re-run the "
                f"dry run and inspect."
            )
        db.commit()
        print(f"\nAPPLIED: {updated} renamed, {updated} audit rows written.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    args = ap.parse_args()

    plan = build_plan()
    if not plan:
        print("Nothing to do — no colliding batch-child names.")
        return

    print(f"\n=== {len(plan)} config(s) to rename ===")
    for p in sorted(plan, key=lambda x: x["old"]):
        print(f"  {p['id'][:8]}  {p['old']!r}\n            -> {p['new']!r}")

    validate(plan)

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        return
    apply(plan)


if __name__ == "__main__":
    main()
