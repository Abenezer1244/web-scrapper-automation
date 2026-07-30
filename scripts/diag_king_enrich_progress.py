"""READ-ONLY: King enrich progress for one job (mailing/phone fill-rate + elapsed).

    railway run --service worker python scripts/diag_king_enrich_progress.py <job_id>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.db.session import system_sync_session


def main() -> None:
    job_id = sys.argv[1]
    with system_sync_session() as db:
        j = db.execute(
            text(
                "SELECT status, record_count, retry_count, started_at, finished_at, "
                "now() AS now, EXTRACT(EPOCH FROM (now() - started_at)) AS elapsed_s "
                "FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).first()
        if not j:
            print(f"NOT FOUND: {job_id}")
            return
        agg = db.execute(
            text(
                "SELECT count(*) AS total, "
                "count(mailing_address) AS have_mailing, "
                "count(phone) AS have_phone, "
                "count(email) AS have_email "
                "FROM results WHERE job_id = :j"
            ),
            {"j": job_id},
        ).first()
        elapsed_min = float(j.elapsed_s or 0) / 60.0
        print(f"status={j.status} retry_count={j.retry_count} elapsed={elapsed_min:.1f}min "
              f"(hard_limit=65min)")
        print(f"now={j.now} started={j.started_at} finished={j.finished_at}")
        print(f"total={agg.total} mailing_filled={agg.have_mailing} "
              f"phone={agg.have_phone} email={agg.have_email}")


if __name__ == "__main__":
    main()
