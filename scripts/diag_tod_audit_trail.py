"""READ-ONLY: show the audit_events rows written by the TOD toggle changes."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session


def main() -> None:
    with system_sync_session() as db:
        rows = db.execute(
            text(
                "SELECT created_at, user_id::text AS user_id, path, detail "
                "FROM audit_events WHERE event = 'scraper_updated' "
                "AND detail LIKE '%include_living_owner_tod%' "
                "ORDER BY created_at DESC LIMIT 10"
            )
        ).all()
    print(f"=== {len(rows)} TOD audit rows ===")
    for r in rows:
        print(f"  {r.created_at}  user={r.user_id[:8]}  via={r.path}")
        print(f"      {r.detail}")


if __name__ == "__main__":
    main()
