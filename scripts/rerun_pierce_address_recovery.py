"""Re-run the Pierce address-recovery step of inline enrichment for an existing job.

Runs the SAME production code path (src/workers/tasks_helpers/enrich.py
pierce_address_recovery) that every new job gets at the end of enrichment:
legal-description parcel repair (free GIS, strict guards) + the assessor (ATIP)
address fallback for parcels the GIS layers cannot resolve. Fill-missing only —
rows that already have an address are untouched; provenance lands in
enrichment_data. Job-log lines are published so the change is visible in the UI.

Usage (worker env — needs DATABASE_URL, REDIS_URL, CAPTCHA_* ):
    railway run --service worker python scripts/rerun_pierce_address_recovery.py <job_id> [--dry-run]

--dry-run prints the rows that WOULD be attempted and exits without any lookup.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _BestEffortPublisher:
    """Redis client wrapper whose publish() degrades to a no-op after the first
    connection failure (e.g. redis.railway.internal from a developer machine)."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._dead = False

    def publish(self, channel: str, message: str) -> int:
        if self._dead:
            return 0
        try:
            return self._inner.publish(channel, message)
        except Exception as exc:  # noqa: BLE001 — best-effort live stream only
            self._dead = True
            print(f"(redis pub/sub unreachable from this host — {str(exc)[:80]}; "
                  "job-log lines are still persisted to the DB)")
            return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    if not args:
        print(__doc__)
        return 2
    job_id = args[0]

    import redis as sync_redis
    from sqlalchemy import select

    from src.config import settings
    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session
    from src.workers.tasks_helpers.enrich import pierce_address_recovery

    with system_sync_session() as db:
        job = db.get(Job, job_id)
        if job is None:
            print(f"job {job_id} not found")
            return 1
        config = db.get(ScraperConfig, job.scraper_config_id)
        if config is None or config.county.lower() != "pierce" or config.state.upper() != "WA":
            print(f"job {job_id} is not a Pierce/WA job (config={config and config.county})")
            return 1
        # Tenant-scoped exactly like the inline path: this job's rows, this job's user.
        all_results = db.execute(
            select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
        ).scalars().all()
        targets = [
            res for res in all_results
            if not res.property_address and res.parcel_id and len(res.parcel_id.strip()) >= 6
        ]
        print(f"job {job_id[:8]} ({config.name!r} {config.county}/{config.record_type}): "
              f"{len(all_results)} rows, {len(targets)} with a parcel but no property address")
        for res in targets:
            print(f"   {res.id[:8]} {res.date_recorded} parcel={res.parcel_id} legal={res.legal_description!r}")
        if dry_run or not targets:
            return 0

        # Job-log lines go to Redis pub/sub (live SSE) AND the job_logs table. From
        # outside Railway's private network the Redis host may not resolve; the
        # live stream is irrelevant for a re-run, but the DB rows must still land,
        # so publishing is best-effort here (the worker path is unchanged).
        r = _BestEffortPublisher(sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs()))
        pierce_address_recovery(db, r, job_id, config, all_results)

        db.expire_all()
        after = db.execute(
            select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
        ).scalars().all()
        # Same authoritative owner-location recompute the worker runs after inline
        # enrichment (tasks.py) — a freshly filled mailing address changes
        # absentee / out-of-state for that row.
        from src.utils.address_intel import compute_owner_flags
        flags_changed = 0
        for res in after:
            flags = compute_owner_flags(res.property_address, res.mailing_address)
            if (res.property_state, res.owner_state, res.absentee_owner, res.out_of_state_owner) != (
                flags["property_state"], flags["owner_state"], flags["absentee_owner"], flags["out_of_state_owner"]
            ):
                res.property_state = flags["property_state"]
                res.owner_state = flags["owner_state"]
                res.absentee_owner = flags["absentee_owner"]
                res.out_of_state_owner = flags["out_of_state_owner"]
                flags_changed += 1
        if flags_changed:
            db.commit()
        print(f"owner flags recomputed for {flags_changed} rows")
        filled = [res for res in after if res.id in {t.id for t in targets} and res.property_address]
        print(f"filled {len(filled)}/{len(targets)}:")
        for res in filled:
            ed = res.enrichment_data or {}
            src = ed.get("address_source") or ("gis_legal_match" if ed.get("parcel_source") == "gis_legal_match" else "?")
            print(f"   {res.id[:8]} parcel={res.parcel_id} prop={res.property_address!r} "
                  f"mail={res.mailing_address!r} via={src} method={ed.get('gis_match_method', '')}")
        still = [res for res in after if res.id in {t.id for t in targets} and not res.property_address]
        for res in still:
            print(f"   UNRESOLVED {res.id[:8]} parcel={res.parcel_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
