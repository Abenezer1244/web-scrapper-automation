"""The watchdog's check that no run finishes DONE without a recorded records_found.

Real test DB and the real ops-alert audit row; nothing is stubbed.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.health import _alert_done_without_count


def test_recent_done_without_count_alerts_once_and_old_rows_do_not():
    now = datetime.now(UTC)
    with SyncSessionLocal() as s:
        user = User(
            id=str(uuid.uuid4()), email=f"dwc_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"), plan="pro",
            records_used=0, records_limit=1000,
        )
        s.add(user)
        s.flush()
        cfg = ScraperConfig(
            id=str(uuid.uuid4()), user_id=user.id, name="dwc", county="pierce", state="WA",
            record_type="trustee_sale", fields=["party_name"], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
        )
        s.add(cfg)
        s.flush()

        def job(finished_ago, found):
            j = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=cfg.id,
                    status="done", finished_at=now - finished_ago, records_found=found)
            s.add(j)
            return j.id

        recent_unknown = job(timedelta(hours=1), None)
        recent_zero = job(timedelta(hours=1), 0)       # a real zero is not unknown
        old_unknown = job(timedelta(days=10), None)    # pre-099 history stays quiet
        s.commit()
        uid, cid = user.id, cfg.id

    try:
        found = _alert_done_without_count(now)
        assert recent_unknown in found
        assert recent_zero not in found and old_unknown not in found
        with SyncSessionLocal() as s:
            alerts = s.execute(text(
                "SELECT count(*) FROM audit_events WHERE event = 'ops_alert' "
                "AND path = 'job_done_without_count:jobs' AND created_at >= :t"),
                {"t": now - timedelta(minutes=1)}).scalar_one()
        assert alerts == 1
    finally:
        with SyncSessionLocal() as s:
            s.execute(delete(Job).where(Job.scraper_config_id == cid))
            s.execute(delete(ScraperConfig).where(ScraperConfig.id == cid))
            s.execute(delete(User).where(User.id == uid))
            s.commit()
