### Task 11: Reconciliation wrappers + hooks

**Files:**
- Modify: `src/api/entitlements.py` (add async + sync apply wrappers)
- Modify: `src/api/routes/billing.py:814,828` (call async wrapper after plan change)
- Modify: `src/workers/scheduler_helpers/billing.py:66` (call sync wrapper after trial downgrade)
- Test: `tests/test_reconciliation_apply.py` (create, real DB)

**Interfaces:**
- Consumes: `plan_reconciliation`, `ConfigRow`, `PAUSED_REASON_ENTITLEMENT`.
- Produces:
  - `async def apply_reconciliation_async(db: AsyncSession, user_id: str, plan: str) -> tuple[int, int]`
  - `def apply_reconciliation_sync(db: Session, user_id: str, plan: str) -> tuple[int, int]`
  - both return `(paused_count, revived_count)`.

- [ ] **Step 1: Append the apply wrappers to `src/api/entitlements.py`**

```python
def _load_all_rows_sync(db, user_id: str) -> list[ConfigRow]:
    from src.db.models import ScraperConfig
    from sqlalchemy import select
    res = db.execute(
        select(
            ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
            ScraperConfig.record_type, ScraperConfig.created_at,
            ScraperConfig.active, ScraperConfig.paused_reason,
        ).where(ScraperConfig.user_id == user_id)
    ).all()
    return [ConfigRow(*r) for r in res]


def apply_reconciliation_sync(db, user_id: str, plan: str) -> tuple[int, int]:
    """Pause now-over-limit configs and revive entitlement-paused ones for `plan`.
    Caller commits. Idempotent: re-running yields (0, 0)."""
    from src.db.models import ScraperConfig
    from sqlalchemy import update
    rows = _load_all_rows_sync(db, user_id)
    pause_ids, revive_ids = plan_reconciliation(rows, plan)
    if pause_ids:
        db.execute(
            update(ScraperConfig)
            .where(ScraperConfig.id.in_(pause_ids))
            .values(active=False, paused_reason=PAUSED_REASON_ENTITLEMENT)
        )
    if revive_ids:
        db.execute(
            update(ScraperConfig)
            .where(ScraperConfig.id.in_(revive_ids))
            .values(active=True, paused_reason=None)
        )
    if pause_ids or revive_ids:
        _logger.info("reconcile user=%s plan=%s paused=%d revived=%d",
                     user_id, plan, len(pause_ids), len(revive_ids))
    return len(pause_ids), len(revive_ids)


async def apply_reconciliation_async(db, user_id: str, plan: str) -> tuple[int, int]:
    """Async twin of apply_reconciliation_sync for the Stripe webhook path."""
    from src.db.models import ScraperConfig
    from sqlalchemy import select, update
    res = await db.execute(
        select(
            ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
            ScraperConfig.record_type, ScraperConfig.created_at,
            ScraperConfig.active, ScraperConfig.paused_reason,
        ).where(ScraperConfig.user_id == user_id)
    )
    rows = [ConfigRow(*r) for r in res.all()]
    pause_ids, revive_ids = plan_reconciliation(rows, plan)
    if pause_ids:
        await db.execute(
            update(ScraperConfig).where(ScraperConfig.id.in_(pause_ids))
            .values(active=False, paused_reason=PAUSED_REASON_ENTITLEMENT)
        )
    if revive_ids:
        await db.execute(
            update(ScraperConfig).where(ScraperConfig.id.in_(revive_ids))
            .values(active=True, paused_reason=None)
        )
    if pause_ids or revive_ids:
        _logger.info("reconcile(async) user=%s plan=%s paused=%d revived=%d",
                     user_id, plan, len(pause_ids), len(revive_ids))
    return len(pause_ids), len(revive_ids)
```

- [ ] **Step 2: Write the failing real-DB test**

```python
# tests/test_reconciliation_apply.py
import os
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DB")


def test_downgrade_pauses_then_upgrade_revives(db_session, user_factory, config_factory):
    # Business user, 2 counties + a divorce config
    user = user_factory(plan="business")
    config_factory(user, county="King", record_type="probate")
    c_div = config_factory(user, county="Pierce", record_type="divorce")
    from src.api.entitlements import apply_reconciliation_sync, PAUSED_REASON_ENTITLEMENT
    paused, revived = apply_reconciliation_sync(db_session, user.id, "pro")
    db_session.commit()
    db_session.refresh(c_div)
    assert paused == 1 and revived == 0
    assert c_div.active is False and c_div.paused_reason == PAUSED_REASON_ENTITLEMENT
    # upgrade back
    paused2, revived2 = apply_reconciliation_sync(db_session, user.id, "business")
    db_session.commit()
    db_session.refresh(c_div)
    assert revived2 == 1 and c_div.active is True and c_div.paused_reason is None
```

(Use the repo's actual fixtures; if none exist for users/configs, build minimal real rows in the test and clean them up — no mocks.)

- [ ] **Step 3: Run test (against dev DB)**

Run: `pytest tests/test_reconciliation_apply.py -v`
Expected: PASS.

- [ ] **Step 4: Hook into the Stripe webhook handlers** in `src/api/routes/billing.py`:

In `_handle_subscription_updated` after `await db.flush()` (line 816) and in `_handle_subscription_deleted` after `await db.flush()` (line 830), add:

```python
        from src.api.entitlements import apply_reconciliation_async
        await apply_reconciliation_async(db, str(user.id), user.plan)
```

- [ ] **Step 5: Hook into trial-expiry** in `src/workers/scheduler_helpers/billing.py` inside the `for user in expired:` loop after `user.records_limit = settings.PLAN_LIMITS["starter"]`:

```python
                from src.api.entitlements import apply_reconciliation_sync
                apply_reconciliation_sync(db, str(user.id), "starter")
```

(Confirm the loop commits afterward — the existing `_expire_trials_impl` commits at function end; reconciliation rides that commit.)

- [ ] **Step 6: Import smoke + test**

Run: `python -c "import src.api.routes.billing, src.workers.scheduler_helpers.billing" && pytest tests/test_reconciliation_apply.py -v`
Expected: clean import + PASS.

- [ ] **Step 7: Commit**

```bash
git add src/api/entitlements.py src/api/routes/billing.py src/workers/scheduler_helpers/billing.py tests/test_reconciliation_apply.py
git commit -m "feat(entitlements): downgrade reconciliation on Stripe + trial expiry (Phase 5)"
```

**PHASE 5 GATE:** full `pytest tests/ -k entitlement or reconciliation` green; Codex `codex review`. User approval before Phase 6.

---

