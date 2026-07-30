"""READ-ONLY: list probate scraper configs that predate the living-owner TOD toggle.

Grandfathered == include_living_owner_tod IS NULL, which the worker treats as
"include TOD" (tasks.py gates on `is False`). Prints owner + config identity so a
human can confirm WHICH config before anything is changed. Writes nothing.

    railway run --service worker python scripts/diag_tod_grandfathered.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session


def main() -> None:
    with system_sync_session() as db:
        rows = db.execute(
            text(
                "SELECT sc.id::text AS id, sc.name, sc.county, sc.state, "
                "sc.include_living_owner_tod AS tod, sc.active, sc.paused_reason, "
                "sc.created_at, u.email, u.is_admin "
                "FROM scraper_configs sc JOIN users u ON u.id = sc.user_id "
                "WHERE sc.record_type = 'probate' "
                "ORDER BY (sc.include_living_owner_tod IS NULL) DESC, sc.created_at"
            )
        ).all()

    grandfathered = [r for r in rows if r.tod is None]
    print(f"=== probate configs: {len(rows)} total, {len(grandfathered)} GRANDFATHERED ===\n")
    for r in rows:
        if r.tod is None:
            state = "GRANDFATHERED -> currently INCLUDES living-owner TOD"
        elif r.tod is False:
            state = "deaths-only"
        else:
            state = "explicitly includes TOD"
        print(
            f"  cfg={r.id} owner={r.email} admin={r.is_admin} "
            f"{r.county}/{r.state} active={r.active} paused={r.paused_reason}"
        )
        print(f"      name={r.name!r}  tod={r.tod!s}  -> {state}")
        print(f"      created={r.created_at}")


if __name__ == "__main__":
    main()
