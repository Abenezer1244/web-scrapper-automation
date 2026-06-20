"""Read-only: list every active county connector whose record_types include
'divorce', plus its base_url and scraper_mode. Used to scope the divorce
hardening campaign's live-test set. Run via: railway run python scripts/diag_divorce_connectors.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.db.models import CountyConnector  # noqa: E402
from src.db.session import SyncSessionLocal  # noqa: E402


def main() -> int:
    with SyncSessionLocal() as db:
        rows = db.execute(
            select(CountyConnector).order_by(CountyConnector.state, CountyConnector.county)
        ).scalars().all()

    print(f"{'county':<14}{'st':<4}{'active':<8}{'divorce?':<10}{'mode':<10}base_url")
    print("-" * 100)
    divorce_active = []
    for c in rows:
        rts = c.record_types or []
        has_div = "divorce" in [r.lower() for r in rts]
        mark = "YES" if has_div else "-"
        if has_div and c.active:
            divorce_active.append((c.county, c.state, getattr(c, "scraper_mode", "?"), c.base_url))
        print(f"{c.county:<14}{c.state:<4}{str(c.active):<8}{mark:<10}{getattr(c,'scraper_mode','?'):<10}{c.base_url}")

    print("\n=== ACTIVE + divorce in record_types ===")
    for county, state, mode, url in divorce_active:
        print(f"  {county}/{state}  mode={mode}  {url}")
    print(f"\nTOTAL active divorce connectors: {len(divorce_active)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
