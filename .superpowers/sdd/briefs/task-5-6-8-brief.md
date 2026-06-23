### Task 5: Guard scheduled single-dispatch

**Files:**
- Modify: `src/workers/scheduler_helpers/dispatch.py` (after user load, ~line 60)

- [ ] **Step 1: Insert after the record-limit skip block** (after current line 66, before the idempotency check):

```python
            # Execution-time entitlement guard (audit until ENTITLEMENT_ENFORCEMENT).
            from src.api.entitlements import ConfigRow, config_run_violation, should_block_run
            _active = db.execute(
                select(
                    ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                    ScraperConfig.record_type, ScraperConfig.created_at,
                    ScraperConfig.active, ScraperConfig.paused_reason,
                ).where(ScraperConfig.user_id == config.user_id, ScraperConfig.active)
            ).all()
            _violation = config_run_violation(
                user.plan if user else "starter", config.state, config.county,
                config.record_type, [ConfigRow(*r) for r in _active],
            )
            if should_block_run(_violation, user_id=str(config.user_id),
                                 plan=(user.plan if user else "starter"), context="schedule_single"):
                continue
```

- [ ] **Step 2: Import smoke**

Run: `python -c "import src.workers.scheduler_helpers.dispatch"`
Expected: clean import.

- [ ] **Step 3: Commit**

```bash
git add src/workers/scheduler_helpers/dispatch.py
git commit -m "feat(entitlements): guard scheduled single dispatch, audit mode (Phase 3)"
```


### Task 6: Guard batch fan-out

**Files:**
- Modify: `src/workers/batch_tasks.py` (in the `for c in configs:` loop, ~line 141)

- [ ] **Step 1: Insert at the top of the `for c in configs:` loop** (before `job = Job(...)` at line 142):

```python
                    from src.api.entitlements import (
                        ConfigRow, config_run_violation, should_block_run,
                    )
                    _active = db.execute(
                        select(
                            ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                            ScraperConfig.record_type, ScraperConfig.created_at,
                            ScraperConfig.active, ScraperConfig.paused_reason,
                        ).where(ScraperConfig.user_id == c.user_id, ScraperConfig.active)
                    ).all()
                    _violation = config_run_violation(
                        user.plan if user else "starter", c.state, c.county,
                        c.record_type, [ConfigRow(*r) for r in _active],
                    )
                    if should_block_run(_violation, user_id=str(c.user_id),
                                        plan=(user.plan if user else "starter"), context="batch_fanout"):
                        continue
```

(`user` was loaded at line 119 `user = db.get(User, batch.user_id)`. Confirm `select` + `ScraperConfig` are imported in this module; if not, add `from src.db.models import ScraperConfig` and `from sqlalchemy import select`.)

- [ ] **Step 2: Import smoke**

Run: `python -c "import src.workers.batch_tasks"`
Expected: clean import.

- [ ] **Step 3: Commit**

```bash
git add src/workers/batch_tasks.py
git commit -m "feat(entitlements): guard batch fan-out children, audit mode (Phase 3)"
```


### Task 8: Guard API-key use for downgraded users

**Files:**
- Modify: `src/api/auth.py` (after `user_match` resolved, ~line 298)
- Test: `tests/test_api_key_plan_guard.py` (create)

**Interfaces:**
- Consumes: `BUSINESS_FEATURES_PLANS` (API access is a Business+ capability).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_key_plan_guard.py
from src.config.constants import BUSINESS_FEATURES_PLANS


def test_api_access_is_business_plus():
    assert "business" in BUSINESS_FEATURES_PLANS
    assert "agency" in BUSINESS_FEATURES_PLANS
    assert "pro" not in BUSINESS_FEATURES_PLANS
    assert "starter" not in BUSINESS_FEATURES_PLANS
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_api_key_plan_guard.py -v`
Expected: PASS (asserts the capability set the guard relies on).

- [ ] **Step 3: Insert the plan check** right after `user_match` is resolved and the `None` check (after line 299 region, where the existing code raises for `user_match is None`):

```python
        # API ACCESS is a Business+ capability. A key minted while on Business
        # must stop working after a downgrade (gated by the enforcement flag so
        # audit mode only logs). Mirrors the create-time require_plan gate.
        if user_match is not None:
            from src.config.constants import BUSINESS_FEATURES_PLANS
            if (user_match.plan or "starter").lower() not in BUSINESS_FEATURES_PLANS:
                if settings.ENTITLEMENT_ENFORCEMENT:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="API access requires a Business or Agency plan.",
                    )
                _logger.info(
                    "entitlement audit user=%s plan=%s context=api_key_use would_block: API access requires Business+",
                    user_match.id, (user_match.plan or "starter"),
                )
```

(Confirm `settings`, `HTTPException`, `status`, and a module `_logger` are imported in `auth.py`; add `from src.config.settings import settings` / logger if missing.)

- [ ] **Step 4: Run test + import smoke**

Run: `pytest tests/test_api_key_plan_guard.py -v && python -c "import src.api.auth"`
Expected: PASS + clean import.

- [ ] **Step 5: Commit**

```bash
git add src/api/auth.py tests/test_api_key_plan_guard.py
git commit -m "feat(entitlements): API-key use rechecks current plan, audit mode (Phase 3)"
```

