"""READ-ONLY: drill into the two findings from the build health sweep.

  A. 6 active connectors reporting health_status='down'
  B. 15,954 / 19,451 results (82%) have no party_name

    railway run --service worker python scripts/diag_xcheck_followup.py
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

        print(f"{_R}\nA. THE 6 DOWN CONNECTORS\n{_R}")
        for r in q(
            # county_connectors has NO last_error / consecutive_failures column —
            # the canary records only health_status + last_checked (verified in models.py).
            "SELECT county, state, scraper_class, scraper_mode, render_mode, "
            "record_types::text rt, health_status, last_checked, base_url "
            "FROM county_connectors WHERE active AND health_status='down' ORDER BY county"
        ):
            print(f"\n  {r.county}/{r.state}  class={r.scraper_class}  mode={r.scraper_mode}/{r.render_mode}")
            print(f"    types={r.rt}")
            print(f"    last_checked={r.last_checked}  url={str(r.base_url)[:64]}")

        print(f"\n{_R}\nA2. are any DOWN connectors actually in use by a live scraper?\n{_R}")
        rows = q(
            "SELECT cc.county, cc.record_types::text rt, count(sc.id) cfgs, "
            "count(sc.id) FILTER (WHERE sc.active) active_cfgs "
            "FROM county_connectors cc "
            "LEFT JOIN scraper_configs sc ON lower(sc.county)=lower(cc.county) "
            "WHERE cc.active AND cc.health_status='down' GROUP BY 1,2 ORDER BY 1"
        )
        for r in rows:
            mark = "  <-- USER-FACING" if r.active_cfgs else ""
            print(f"  {r.county:<12} configs={r.cfgs} active={r.active_cfgs}{mark}")

        print(f"\n{_R}\nB. MISSING party_name - by record type + county\n{_R}")
        for r in q(
            "SELECT sc.record_type, sc.county, count(*) n, "
            "count(*) FILTER (WHERE r.party_name IS NULL OR r.party_name='') missing, "
            "round(100.0 * count(*) FILTER (WHERE r.party_name IS NULL OR r.party_name='') "
            "      / nullif(count(*),0), 1) pct "
            "FROM results r JOIN jobs j ON j.id=r.job_id "
            "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "GROUP BY 1,2 ORDER BY missing DESC"
        ):
            print(f"  {r.record_type:<18} {r.county:<12} {r.missing:>6} / {r.n:<6} missing ({r.pct}%)")

        print(f"\n{_R}\nB2. do those rows carry an owner name ELSEWHERE (enrichment_data)?\n{_R}")
        for r in q(
            "SELECT sc.record_type, sc.county, count(*) n, "
            "count(*) FILTER (WHERE r.enrichment_data::text ILIKE '%owner%') has_owner_key "
            "FROM results r JOIN jobs j ON j.id=r.job_id "
            "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "WHERE r.party_name IS NULL OR r.party_name='' "
            "GROUP BY 1,2 ORDER BY n DESC"
        ):
            print(f"  {r.record_type:<18} {r.county:<12} {r.n:>6} blank, {r.has_owner_key} have an owner key in enrichment_data")

        print(f"\n{_R}\nB3. sample of blank-name rows (is the lead still usable?)\n{_R}")
        for r in q(
            "SELECT sc.county, sc.record_type, r.parcel_id, "
            "left(coalesce(r.property_address,''), 38) addr, r.date_recorded, "
            "left(r.enrichment_data::text, 90) enr "
            "FROM results r JOIN jobs j ON j.id=r.job_id "
            "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "WHERE (r.party_name IS NULL OR r.party_name='') LIMIT 6"
        ):
            print(f"  {r.county}/{r.record_type} parcel={r.parcel_id} addr='{r.addr}' rec={r.date_recorded}")
            print(f"     enrichment={r.enr}")

        print(f"\n{_R}\nB4. same view for property_address\n{_R}")
        for r in q(
            "SELECT sc.record_type, sc.county, count(*) n, "
            "count(*) FILTER (WHERE r.property_address IS NULL OR r.property_address='') missing "
            "FROM results r JOIN jobs j ON j.id=r.job_id "
            "JOIN scraper_configs sc ON sc.id=j.scraper_config_id "
            "GROUP BY 1,2 HAVING count(*) FILTER (WHERE r.property_address IS NULL OR r.property_address='') > 0 "
            "ORDER BY missing DESC"
        ):
            print(f"  {r.record_type:<18} {r.county:<12} {r.missing:>6} / {r.n} missing address")


if __name__ == "__main__":
    main()
