"""Read-only data-quality report: coverage by county x record type, and per-job
warnings against the county baseline. Admin tool; prints, never writes.

    railway run --service worker python scripts/data_quality_report.py [--days 90] [--jobs 40]

Uses the worker's cross-tenant session (every tenant's rows are read, by design:
this is an operational health view, no PII is printed). The same measurement the
hourly beat sweep applies (src/workers/data_quality.py), so a number here is the
number an alert would have fired on.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.db.session import system_sync_session
from src.workers.data_quality import FIELDS, check_job

_COVERAGE = """
SELECT lower(sc.county) AS county, sc.record_type,
       count(*) AS leads,
       count(r.parcel_id) FILTER (WHERE r.parcel_id <> '') AS parcel_id,
       count(r.property_address) FILTER (WHERE r.property_address <> '') AS property_address,
       count(r.mailing_address) FILTER (WHERE r.mailing_address <> '') AS mailing_address,
       count(r.phone) AS phone, count(r.email) AS email,
       count(r.auction_date) AS auction_date, count(r.default_amount) AS default_amount,
       count(*) FILTER (WHERE r.mailing_address IS NOT NULL AND r.property_address IS NOT NULL
                          AND upper(btrim(r.mailing_address)) = upper(btrim(r.property_address))) AS echo
FROM results r
JOIN jobs j ON j.id = r.job_id
JOIN scraper_configs sc ON sc.id = j.scraper_config_id
WHERE j.status = 'done' AND NOT r.is_duplicate AND j.finished_at >= :since
GROUP BY 1, 2 ORDER BY 1, 2
"""

_RECENT = """
SELECT j.id FROM jobs j WHERE j.status = 'done' AND j.finished_at >= :since
ORDER BY j.finished_at DESC LIMIT :limit
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--jobs", type=int, default=40, help="most recent done jobs to judge")
    args = ap.parse_args()
    since = datetime.now(UTC) - timedelta(days=args.days)
    with system_sync_session() as db:
        print(f"Coverage of NEW rows by county x record type, done jobs since {since:%Y-%m-%d}")
        head = f"{'county':<12}{'type':<17}{'leads':>7}" + "".join(f"{f[:9]:>10}" for f in FIELDS) + f"{'echo%':>7}"
        print(head)
        for r in db.execute(text(_COVERAGE), {"since": since}).mappings():
            n = r["leads"] or 1
            mail = r["mailing_address"] or 1
            print(f"{r['county']:<12}{r['record_type']:<17}{r['leads']:>7}"
                  + "".join(f"{100 * (r[f] or 0) / n:>9.1f}%" for f in FIELDS)
                  + f"{100 * (r['echo'] or 0) / mail:>6.0f}%")
        print(f"\nMost recent {args.jobs} done jobs judged against their baseline:")
        for row in db.execute(text(_RECENT), {"since": since, "limit": args.jobs}).mappings():
            rep = check_job(db, str(row["id"]), alert=False)  # report only, never e-mails
            if rep.get("skipped"):
                continue
            flag = "WARN" if rep["warnings"] else "ok  "
            run = rep["run"]
            print(f"{flag} {rep['job_id'][:8]} {rep['county']:<10}{rep['record_type']:<16} rows={run['rows']:>5} "
                  f"parcel={run['pct']['parcel_id']:>5.1f}% prop={run['pct']['property_address']:>5.1f}% "
                  f"mail={run['pct']['mailing_address']:>5.1f}% (base {rep['baseline']['pct']['mailing_address']:>5.1f}% "
                  f"over {rep['baseline']['rows']} rows) echo={run['echo_pct']:.0f}%"
                  + ("".join(f"\n       ! {w['kind']} {w['field']} {w['run_pct']}% vs {w['baseline_pct']}%"
                             for w in rep["warnings"])))


if __name__ == "__main__":
    main()
