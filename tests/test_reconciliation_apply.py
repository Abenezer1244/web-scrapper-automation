"""Reconciliation apply — real-DB test, SKIPPED by default. Run only against a
dedicated test DB with RUN_DB_TESTS=1 (never the prod .env). See Phase 7."""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="needs a dedicated test DB; set RUN_DB_TESTS=1 (never against prod)",
)


@pytest.mark.asyncio
async def test_downgrade_pauses_then_upgrade_revives(db, business_user):
    from src.api.entitlements import PAUSED_REASON_ENTITLEMENT, apply_reconciliation_async
    from src.db.models import ScraperConfig

    # business_user (plan=business) gets a divorce config (premium type)
    cfg = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=business_user.id,
        name="t",
        county="King",
        state="WA",
        record_type="divorce",
        fields=[],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    # downgrade to pro → divorce not allowed → paused
    paused, revived = await apply_reconciliation_async(db, str(business_user.id), "pro")
    await db.commit()
    await db.refresh(cfg)
    assert paused == 1 and revived == 0
    assert cfg.active is False and cfg.paused_reason == PAUSED_REASON_ENTITLEMENT
    # upgrade back to business → revived
    paused2, revived2 = await apply_reconciliation_async(db, str(business_user.id), "business")
    await db.commit()
    await db.refresh(cfg)
    assert revived2 == 1 and cfg.active is True and cfg.paused_reason is None
