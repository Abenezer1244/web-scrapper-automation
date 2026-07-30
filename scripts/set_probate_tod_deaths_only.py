"""Flip ONE probate scraper config to deaths-and-inheritances-only.

Sets ``include_living_owner_tod = False`` on a single config, mirroring what
PATCH /scrapers/{id} does (src/api/routes/scrapers.py):
  * writes through the ORM so ``updated_at``'s ``onupdate=func.now()`` fires,
  * inserts the same ``scraper_updated`` row into ``audit_events`` that
    ``audit_log()`` would have persisted, with the identical detail string.

DRY RUN BY DEFAULT. Pass --apply to actually write.

    railway run --service worker python scripts/set_probate_tod_deaths_only.py <config_id>
    railway run --service worker python scripts/set_probate_tod_deaths_only.py <config_id> --apply

Guards (all must hold, else it refuses):
  * config exists and record_type == 'probate' (the column is probate-only)
  * include_living_owner_tod IS currently NULL (grandfathered) — refuses to
    overwrite an explicit True/False someone deliberately chose
  * exactly one config id, passed explicitly — never a bulk sweep, so another
    tenant's config can't be caught by accident
"""
import os
import sys
import uuid as _uuid
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.models import AuditEvent, ScraperConfig
from src.db.session import system_sync_session

_TOD_RECORD_TYPE = "probate"


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    if len(args) != 1:
        print(__doc__)
        return 2
    config_id = args[0]

    with system_sync_session() as db:
        cfg = db.get(ScraperConfig, config_id)
        if cfg is None:
            print(f"REFUSED: no scraper_config with id={config_id}")
            return 1
        if cfg.record_type != _TOD_RECORD_TYPE:
            print(f"REFUSED: record_type={cfg.record_type!r}, not {_TOD_RECORD_TYPE!r}. "
                  "include_living_owner_tod is probate-only.")
            return 1
        if cfg.include_living_owner_tod is not None:
            print(f"REFUSED: include_living_owner_tod is already "
                  f"{cfg.include_living_owner_tod!r} (explicitly set). Not overwriting "
                  "a deliberate choice.")
            return 1

        print(f"config    {cfg.id}")
        print(f"name      {cfg.name!r}")
        print(f"owner     {cfg.user_id}")
        print(f"target    {cfg.county}/{cfg.state} {cfg.record_type} active={cfg.active}")
        print(f"change    include_living_owner_tod: {cfg.include_living_owner_tod!s} -> False")
        print("effect    stops ingesting living-owner Transfer-on-Death deeds "
              "(estate-planning filings); keeps deaths + inheritances")

        if not apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
            return 0

        # Mirror the route's audit detail string exactly.
        detail = (
            f"config_id={cfg.id} changed=include_living_owner_tod "
            f"include_living_owner_tod={cfg.include_living_owner_tod}->False"
        )
        cfg.include_living_owner_tod = False
        db.add(
            AuditEvent(
                id=str(_uuid.uuid4()),
                event="scraper_updated",
                user_id=str(cfg.user_id),
                ip=None,
                path="scripts/set_probate_tod_deaths_only.py",
                detail=detail[:512],
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        db.refresh(cfg)
        print(f"\nAPPLIED. include_living_owner_tod={cfg.include_living_owner_tod!s} "
              f"updated_at={cfg.updated_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
