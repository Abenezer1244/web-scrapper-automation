"""READ-ONLY: whole-build health sweep against production.

Looks for things that are actually broken right now, rather than reading PR
titles: failing/stuck jobs, degraded connectors, scrapers that have silently
stopped producing, data-integrity anomalies, and stranded batch runs.

    railway run --service worker python scripts/diag_build_health_sweep.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402

_RULE = "-" * 74


def section(t: str) -> None:
    print(f"\n{_RULE}\n{t}\n{_RULE}")


def main() -> None:
    with system_sync_session() as db:
        q = lambda s, p=None: db.execute(text(s), p or {}).all()  # noqa: E731

        section("1. JOB OUTCOMES - last 30 days")
        for r in q(
            "SELECT status, count(*) n, max(created_at) newest FROM jobs "
            "WHERE created_at > now() - interval '30 days' GROUP BY status ORDER BY n DESC"
        ):
            print(f"  {r.status:<12} {r.n:>5}   newest={r.newest}")

        section("2. STUCK JOBS - non-terminal and older than 2h")
        stuck = q(
            "SELECT j.id::text id, j.status, j.created_at, sc.name, sc.county, sc.record_type "
            "FROM jobs j LEFT JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
            "WHERE j.status NOT IN ('done','failed','cancelled') "
            "AND j.created_at < now() - interval '2 hours' ORDER BY j.created_at"
        )
        print(f"  {len(stuck)} stuck")
        for r in stuck[:12]:
            print(f"    {r.created_at}  {r.status:<10} {r.county}/{r.record_type}  {(r.name or '')[:34]}")

        section("3. RECENT FAILURES - last 14 days")
        fails = q(
            "SELECT sc.county, sc.record_type, count(*) n, max(j.error_message) sample "
            "FROM jobs j LEFT JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
            "WHERE j.status = 'failed' AND j.created_at > now() - interval '14 days' "
            "GROUP BY 1,2 ORDER BY n DESC LIMIT 12"
        )
        print(f"  {len(fails)} failing county/type pairs")
        for r in fails:
            print(f"    {r.county}/{r.record_type}  x{r.n}")
            print(f"       {str(r.sample or '')[:96]}")

        section("4. CONNECTOR HEALTH")
        for r in q(
            "SELECT health_status, count(*) n FROM county_connectors "
            "WHERE active GROUP BY 1 ORDER BY n DESC"
        ):
            print(f"  {str(r.health_status):<12} {r.n:>4}")
        bad = q(
            "SELECT county, state, record_types::text rt, health_status, last_checked "
            "FROM county_connectors WHERE active AND health_status IS DISTINCT FROM 'healthy' "
            "ORDER BY county"
        )
        print(f"\n  {len(bad)} active connectors NOT healthy:")
        for r in bad[:20]:
            print(f"    {r.county:<12} {r.state}  {str(r.health_status):<10} checked={r.last_checked}")

        section("5. DATA INTEGRITY - results")
        for label, sql in [
            ("total results", "SELECT count(*) v FROM results"),
            ("no parcel_id", "SELECT count(*) v FROM results WHERE parcel_id IS NULL OR parcel_id=''"),
            ("no property_address",
             "SELECT count(*) v FROM results WHERE property_address IS NULL OR property_address=''"),
            ("no party_name", "SELECT count(*) v FROM results WHERE party_name IS NULL OR party_name=''"),
            ("no date_recorded", "SELECT count(*) v FROM results WHERE date_recorded IS NULL"),
            ("with skip-trace", "SELECT count(*) v FROM results WHERE phones IS NOT NULL OR emails IS NOT NULL"),
        ]:
            print(f"  {label:<22} {q(sql)[0].v:>8}")

        section("6. RESULTS BY RECORD TYPE (via job's config)")
        for r in q(
            "SELECT sc.record_type, count(*) n, count(DISTINCT sc.county) counties "
            "FROM results r JOIN jobs j ON j.id = r.job_id "
            "JOIN scraper_configs sc ON sc.id = j.scraper_config_id "
            "GROUP BY 1 ORDER BY n DESC"
        ):
            print(f"  {r.record_type:<20} {r.n:>7} rows across {r.counties} counties")

        section("7. CLARK tax_delinquent - PR #97's claim (1,968 mislabeled)")
        rows = q(
            "SELECT count(*) n FROM results r JOIN jobs j ON j.id=r.job_id "
            "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "WHERE sc.county='clark' AND sc.record_type='tax_delinquent'"
        )
        print(f"  clark/tax_delinquent result rows now: {rows[0].n}")
        cc = q(
            "SELECT county, record_types::text rt, active, health_status "
            "FROM county_connectors WHERE county='clark'"
        )
        for r in cc:
            print(f"  connector clark: active={r.active} health={r.health_status} types={r.rt}")

        section("8. BATCH RUNS - stranded?")
        for r in q(
            "SELECT status, count(*) n, max(created_at) newest FROM batch_runs GROUP BY 1 ORDER BY n DESC"
        ):
            print(f"  {r.status:<12} {r.n:>4}  newest={r.newest}")
        old = q(
            "SELECT count(*) n FROM batch_runs WHERE status IN ('pending','running') "
            "AND created_at < now() - interval '2 hours'"
        )
        print(f"  stranded (pending/running >2h): {old[0].n}")

        section("9. SCRAPERS THAT STOPPED PRODUCING")
        for r in q(
            "SELECT sc.name, sc.county, sc.record_type, sc.active, "
            "max(j.created_at) last_run, "
            "max(j.created_at) FILTER (WHERE j.status='done' AND j.record_count>0) last_good "
            "FROM scraper_configs sc LEFT JOIN jobs j ON j.scraper_config_id=sc.id "
            "WHERE sc.active GROUP BY 1,2,3,4 ORDER BY last_run DESC NULLS LAST"
        ):
            flag = "" if r.last_good else "   <-- never produced rows"
            print(f"  {str(r.county):<10} {str(r.record_type):<18} last_run={r.last_run} {flag}")


if __name__ == "__main__":
    main()
