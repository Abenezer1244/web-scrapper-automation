"""One-time purge of the global skip_trace_cache (per-tenant cache cutover, 2026-06-10).

`address_cache_key` now includes `user_id`, so the cache is per-tenant. Every row
written under the OLD global key (no user_id) is unreachable under the new keys —
no tenant will ever compute that hash — so those rows are dead weight (they are
never read; the 90-day TTL only fires on read). This deletes them for a clean slate;
the per-tenant cache repopulates as tenants scrape.

Safe + idempotent: deleting the cache only forces re-traces (a cost, already
accepted with the per-tenant decision); it never loses tenant Result data (Results
keep their phone/email independently). Re-running after the cache repopulates would
delete fresh per-tenant rows too, so run ONCE right after the per-tenant deploy.

Run out-of-band (NOT a migration — avoids in-migration data churn on the
advisory-locked boot deploy):
    railway run --service worker python scripts/purge_skip_trace_cache.py
"""
import logging

from sqlalchemy import text

from src.db.session import SyncSessionLocal

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("purge_skip_trace_cache")


def run() -> None:
    with SyncSessionLocal() as db:
        before = db.execute(text("SELECT count(*) FROM skip_trace_cache")).scalar()
        db.execute(text("DELETE FROM skip_trace_cache"))
        db.commit()
        after = db.execute(text("SELECT count(*) FROM skip_trace_cache")).scalar()
    _log.info("skip_trace_cache purged: %s rows deleted (now %s).", before, after)


if __name__ == "__main__":
    run()
