# PHASE 3 — Wire guard at every execution point (AUDIT mode)

> All Phase-3 tasks add a check that LOGS in audit mode and BLOCKS only when `settings.ENTITLEMENT_ENFORCEMENT` is True. Add this shared async helper to `src/api/entitlements.py` first (it is the API-side action wrapper):

```python
def enforce_runnable_http(violation: str | None, *, user: User, context: str) -> None:
    """API call sites: raise 402 when enforcement is ON and a violation exists,
    else audit-log. No-op when violation is None."""
    if not violation:
        return
    if settings.ENTITLEMENT_ENFORCEMENT:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Plan limit reached — {violation}. Upgrade your plan to continue.",
        )
    _logger.info(
        "entitlement audit (NOT enforced) user=%s plan=%s context=%s would_block: %s",
        user.id, _plan_of(user), context, violation,
    )


def should_block_run(violation: str | None, *, user_id: str, plan: str, context: str) -> bool:
    """Worker/scheduler call sites: returns True (caller must block/skip/fail) only
    when enforcement is ON and a violation exists; always audit-logs the would-block."""
    if not violation:
        return False
    _logger.info(
        "entitlement audit user=%s plan=%s context=%s would_block: %s",
        user_id, plan, context, violation,
    )
    return settings.ENTITLEMENT_ENFORCEMENT
```

Commit this helper addition with Task 3.

### Task 3: Guard POST /jobs

**Files:**
- Modify: `src/api/routes/jobs.py` (after config load, ~line 89)
- Modify: `src/api/entitlements.py` (add `enforce_runnable_http` + `should_block_run` per above)
- Test: `tests/test_jobs_entitlement_guard.py` (create)

**Interfaces:**
- Consumes: `config_run_violation`, `enforce_runnable_http`, `ConfigRow`.

- [ ] **Step 1: Write the failing test** (real DB; uses existing test fixtures for a user + configs)

```python
# tests/test_jobs_entitlement_guard.py
import pytest
from src.config.settings import settings
from src.api.entitlements import ConfigRow, config_run_violation


def test_guard_blocks_starter_preforeclosure(monkeypatch):
    # Pure-path assertion: the route delegates to config_run_violation, so verify
    # the decision the route will make for a starter user with a pre_foreclosure config.
    rows = [ConfigRow(id="1", state="WA", county="King", record_type="pre_foreclosure",
                      created_at=__import__("datetime").datetime(2026,1,1,tzinfo=__import__("datetime").UTC))]
    assert config_run_violation("starter", "WA", "King", "pre_foreclosure", rows) is not None
```

(Full HTTP integration test against the live app is added in Task 13's live-verify; this unit asserts the decision wired into the route.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_jobs_entitlement_guard.py -v`
Expected: FAIL only if import path wrong; otherwise PASS once Task 2 merged. (This guards regressions.)

- [ ] **Step 3: Insert the guard in `src/api/routes/jobs.py`** immediately after the `config is None` check (after current line 89):

```python
    # Execution-time entitlement guard (audit-mode until ENTITLEMENT_ENFORCEMENT).
    # An existing config can outlive a downgrade; re-validate against CURRENT plan.
    from src.api.entitlements import ConfigRow, config_run_violation, enforce_runnable_http
    active_rows = (await db.execute(
        select(
            ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
            ScraperConfig.record_type, ScraperConfig.created_at,
            ScraperConfig.active, ScraperConfig.paused_reason,
        ).where(ScraperConfig.user_id == current_user.id, ScraperConfig.active)
    )).all()
    rows = [ConfigRow(*r) for r in active_rows]
    enforce_runnable_http(
        config_run_violation(current_user.plan, config.state, config.county, config.record_type, rows),
        user=current_user, context="create_job",
    )
```

(`ScraperConfig.paused_reason` exists after Phase 5 Task 10. If Phase 3 ships before Phase 5, temporarily select a literal: replace `ScraperConfig.paused_reason` with `sa.literal(None)` and note it; the reconciliation column lands in Phase 5. Recommended: run Phase 5 Task 10 migration first if executing out of order.)

- [ ] **Step 4: Run test + import smoke**

Run: `pytest tests/test_jobs_entitlement_guard.py -v && python -c "import src.api.routes.jobs"`
Expected: PASS + clean import.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/jobs.py src/api/entitlements.py tests/test_jobs_entitlement_guard.py
git commit -m "feat(entitlements): guard POST /jobs at execution time, audit mode (Phase 3)"
```

