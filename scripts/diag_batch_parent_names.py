"""READ-ONLY: for every colliding scraper_config, what is its parent batch named?

Decides whether the backfill can rebuild existing duplicate names with the SAME
derive_batch_child_name() the new code uses, or needs a fallback for children
whose parent batch has no name.

    railway run --service worker python scripts/diag_batch_parent_names.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.api.routes.batches import derive_batch_child_name  # noqa: E402
from src.db.session import system_sync_session  # noqa: E402


def main() -> None:
    with system_sync_session() as db:
        rows = db.execute(
            text(
                "SELECT sc.id::text AS id, sc.user_id::text AS user_id, sc.name, "
                "sc.county, sc.record_type, sc.batch_id::text AS batch_id, "
                "sc.created_at, sb.name AS batch_name, sb.created_at AS batch_created "
                "FROM scraper_configs sc "
                "LEFT JOIN scraper_batches sb ON sb.id = sc.batch_id "
                "ORDER BY sc.user_id, sc.name, sc.created_at"
            )
        ).all()

    by_user_name = defaultdict(list)
    for r in rows:
        by_user_name[(r.user_id, (r.name or "").strip().lower())].append(r)

    colliding = [cs for cs in by_user_name.values() if len(cs) > 1]
    print(f"=== {len(rows)} configs, {sum(len(c) for c in colliding)} colliding ===\n")

    no_batch = 0
    no_batch_name = 0
    would_still_collide = 0

    for group in colliding:
        print(f'--- "{group[0].name}" x{len(group)} ---')
        proposed = []
        for c in group:
            if c.batch_id is None:
                kind = "NOT a batch child"
                new = None
                no_batch += 1
            elif not (c.batch_name or "").strip():
                kind = "batch has NO name"
                new = None
                no_batch_name += 1
            else:
                kind = f'batch="{c.batch_name}"'
                new = derive_batch_child_name(c.batch_name, c.county, c.record_type)
            print(f"  id={c.id[:8]} created={c.created_at} {kind}")
            print(f"     proposed -> {new!r}")
            proposed.append(new)
        # Would the rebuilt names still collide with each other?
        real = [p for p in proposed if p]
        if len(real) != len(set(real)):
            would_still_collide += 1
            print("     !! rebuilt names STILL collide within this group")
        print()

    print(f"=== configs with no batch_id: {no_batch} ===")
    print(f"=== configs whose parent batch has no name: {no_batch_name} ===")
    print(f"=== groups that would STILL collide after rebuild: {would_still_collide} ===")


if __name__ == "__main__":
    main()
