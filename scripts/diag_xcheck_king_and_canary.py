"""READ-ONLY: is the King tax_delinquent name gap CURRENT or historical?
And why is snohomish the only DOWN connector with live user scrapers?

    railway run --service worker python scripts/diag_xcheck_king_and_canary.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_R = "-" * 74


def main() -> None:
    with system_sync_session() as db:
        q = lambda s, p=None: db.execute(text(s), p or {}).all()  # noqa: E731

        print(f"{_R}\n1. King tax_delinquent rows BY JOB (newest first)\n{_R}")
        for r in q(
            "SELECT j.id::text jid, j.created_at, j.status, count(r.id) rows, "
            "count(*) FILTER (WHERE r.party_name IS NULL OR r.party_name='') no_name, "
            "count(*) FILTER (WHERE r.property_address IS NULL OR r.property_address='') no_addr "
            "FROM jobs j JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "LEFT JOIN results r ON r.job_id=j.id "
            "WHERE sc.county='king' AND sc.record_type='tax_delinquent' "
            "GROUP BY 1,2,3 ORDER BY j.created_at DESC"
        ):
            print(f"  {r.created_at}  {r.status:<8} rows={r.rows:<6} no_name={r.no_name:<6} no_addr={r.no_addr}")

        print(f"\n{_R}\n2. Snohomish tax_delinquent for contrast (same record type, names present)\n{_R}")
        for r in q(
            "SELECT j.created_at, j.status, count(r.id) rows, "
            "count(*) FILTER (WHERE r.party_name IS NULL OR r.party_name='') no_name "
            "FROM jobs j JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "LEFT JOIN results r ON r.job_id=j.id "
            "WHERE sc.county='snohomish' AND sc.record_type='tax_delinquent' "
            "GROUP BY 1,2 ORDER BY j.created_at DESC LIMIT 5"
        ):
            print(f"  {r.created_at}  {r.status:<8} rows={r.rows:<6} no_name={r.no_name}")

        print(f"\n{_R}\n3. Sample King tax party_name values that ARE set (any at all?)\n{_R}")
        rows = q(
            "SELECT r.party_name, count(*) n FROM results r JOIN jobs j ON j.id=r.job_id "
            "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "WHERE sc.county='king' AND sc.record_type='tax_delinquent' "
            "AND r.party_name IS NOT NULL AND r.party_name <> '' "
            "GROUP BY 1 ORDER BY n DESC LIMIT 5"
        )
        print(f"  distinct non-empty party_name values: {len(rows)}")
        for r in rows:
            print(f"    {r.party_name!r} x{r.n}")

        print(f"\n{_R}\n4. CANARY COVERAGE - when was each connector last checked?\n{_R}")
        for r in q(
            "SELECT county, state, health_status, last_checked, "
            "round(EXTRACT(epoch FROM (now() - last_checked))/3600, 1) hours_ago "
            "FROM county_connectors WHERE active ORDER BY last_checked NULLS FIRST LIMIT 12"
        ):
            flag = "  <-- STALE" if (r.hours_ago or 0) > 12 else ""
            print(f"  {r.county:<12} state={r.state!r:<6} {str(r.health_status):<9} {r.hours_ago}h ago{flag}")

        print(f"\n{_R}\n5. Is `state` case inconsistent across connectors?\n{_R}")
        for r in q("SELECT state, count(*) n FROM county_connectors GROUP BY 1 ORDER BY n DESC"):
            print(f"  state={r.state!r}  {r.n} connectors")

        print(f"\n{_R}\n6. Snohomish connector rows (dupes? which one do scrapers resolve?)\n{_R}")
        for r in q(
            "SELECT id::text id, county, state, record_types::text rt, scraper_class, "
            "active, health_status, last_checked FROM county_connectors "
            "WHERE lower(county)='snohomish' ORDER BY last_checked DESC NULLS LAST"
        ):
            print(f"  {r.id[:8]} state={r.state!r} active={r.active} health={r.health_status}")
            print(f"     types={r.rt} class={(r.scraper_class or '')[:56]}")
            print(f"     last_checked={r.last_checked}")


if __name__ == "__main__":
    main()
