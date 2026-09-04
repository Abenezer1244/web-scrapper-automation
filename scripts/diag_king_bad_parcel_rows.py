"""Read-only: full state of the rows carrying the malformed King parcel 64116000027,
including derived fields and any skip-trace residue."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BAD_PID = "64116000027"


def main():
    from sqlalchemy import select

    from src.db.models import (
        DeliveredRecord,
        Job,
        PendingSkipTraceRow,
        Result,
        ScraperConfig,
    )
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        rows = db.execute(select(Result).where(Result.parcel_id == BAD_PID)).scalars().all()
        for r in rows:
            cfg = db.execute(
                select(ScraperConfig).join(Job, Job.scraper_config_id == ScraperConfig.id)
                .where(Job.id == r.job_id)
            ).scalars().first()
            print(f"--- result {r.id} job={r.job_id} cfg={cfg.name if cfg else '?'} "
                  f"({cfg.record_type if cfg else '?'})")
            print(f"    party={r.party_name!r} heirs={r.heirs!r} doc_type={r.doc_type!r}")
            print(f"    prop={r.property_address!r} mail={r.mailing_address!r}")
            print(f"    city={r.property_city!r} state={r.property_state!r} zip={r.property_zip!r} "
                  f"owner_state={r.owner_state!r} absentee={r.absentee_owner} oos={r.out_of_state_owner}")
            print(f"    dedup_hash={r.dedup_hash} property_key={r.property_key} is_dup={r.is_duplicate}")
            print(f"    skip_trace_status={r.skip_trace_status} phone={'Y' if r.phone else 'N'} "
                  f"email={'Y' if r.email else 'N'}")
            print(f"    enrichment={json.dumps(r.enrichment_data, default=str)}")
            pend = db.execute(
                select(PendingSkipTraceRow).where(PendingSkipTraceRow.result_id == r.id)
            ).scalars().all()
            for p in pend:
                print(f"    PENDING skip-trace {p.id} status={p.status} enqueued={p.enqueued_at} "
                      f"addr={p.property_address!r} city={p.city!r} zip={p.zip!r} "
                      f"name={p.first_name!r}/{p.last_name!r} mail={p.mail_address!r}")
            if r.dedup_hash:
                dr = db.execute(
                    select(DeliveredRecord).where(DeliveredRecord.user_id == r.user_id,
                                                  DeliveredRecord.dedup_hash == r.dedup_hash)
                ).scalars().all()
                print(f"    delivered_records rows: {len(dr)}")


if __name__ == "__main__":
    main()
