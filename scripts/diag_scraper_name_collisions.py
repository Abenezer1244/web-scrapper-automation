"""READ-ONLY: how badly do scraper names collide, per user?

Answers the design question behind the dashboard Scrapers widget: if we add a
"Scraper" name column, do users actually see duplicate names — and which field
(created_at / schedule / doc_types) would tell the duplicates apart?

    railway run --service worker python scripts/diag_scraper_name_collisions.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402


def main() -> None:
    with system_sync_session() as db:
        rows = db.execute(
            text(
                "SELECT sc.id::text AS id, sc.user_id::text AS user_id, sc.name, "
                "sc.county, sc.state, sc.record_type, sc.active, sc.created_at, "
                "sc.schedule::text AS schedule, sc.doc_types::text AS doc_types, "
                "u.email "
                "FROM scraper_configs sc LEFT JOIN users u ON u.id = sc.user_id "
                "ORDER BY sc.user_id, sc.name, sc.created_at"
            )
        ).all()

    print(f"=== {len(rows)} scraper_configs total ===\n")

    # The widget is per-user, so collisions only matter WITHIN one user's list.
    by_user = defaultdict(list)
    for r in rows:
        by_user[(r.user_id, r.email)].append(r)

    total_colliding = 0
    users_affected = 0

    for (uid, email), cfgs in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
        by_name = defaultdict(list)
        for c in cfgs:
            by_name[(c.name or "").strip().lower()].append(c)
        dupes = {n: cs for n, cs in by_name.items() if len(cs) > 1}
        if not dupes:
            continue
        users_affected += 1
        print(f"--- user {email or uid} : {len(cfgs)} scrapers, {len(dupes)} colliding name(s) ---")
        for name, cs in dupes.items():
            total_colliding += len(cs)
            print(f'  name="{cs[0].name}" x{len(cs)}')
            for c in cs:
                # Which candidate disambiguator actually differs across the group?
                print(
                    f"     id={c.id[:8]} active={c.active} created={c.created_at} "
                    f"sched={c.schedule} doc_types={c.doc_types}"
                )
            distinct_created = len({c.created_at for c in cs})
            distinct_sched = len({c.schedule for c in cs})
            distinct_doc = len({c.doc_types for c in cs})
            print(
                f"     -> distinguishes: created_at={distinct_created}/{len(cs)} "
                f"schedule={distinct_sched}/{len(cs)} doc_types={distinct_doc}/{len(cs)}"
            )
        print()

    print(f"=== {users_affected} users affected, {total_colliding} colliding configs ===")

    # Also: how many names are the auto-generated "<County> <record_type>" default?
    auto = [
        r for r in rows
        if (r.name or "").strip().lower() == f"{r.county} {r.record_type}".strip().lower()
    ]
    print(f"=== {len(auto)} configs still carry the auto-generated default name ===")
    for r in auto[:20]:
        print(f"  {r.name!r} owner={r.email} created={r.created_at}")


if __name__ == "__main__":
    main()
