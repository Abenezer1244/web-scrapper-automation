### Task 4: Guard worker job-start (final backstop)

**Files:**
- Modify: `src/workers/tasks.py` (after `user` load, ~line 256)

- [ ] **Step 1: Insert the guard** right after `user = db.execute(...).scalar_one()` (line 256), before the atomic claim:

```python
        # Execution-time entitlement backstop (audit until ENTITLEMENT_ENFORCEMENT).
        # Catches API/scheduled/retry/watchdog paths that bypassed create-time checks.
        from src.api.entitlements import ConfigRow, config_run_violation, should_block_run
        _active = db.execute(
            select(
                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                ScraperConfig.record_type, ScraperConfig.created_at,
                ScraperConfig.active, ScraperConfig.paused_reason,
            ).where(ScraperConfig.user_id == job.user_id, ScraperConfig.active)
        ).all()
        _violation = config_run_violation(
            user.plan, config.state, config.county, config.record_type,
            [ConfigRow(*r) for r in _active],
        )
        if should_block_run(_violation, user_id=str(job.user_id), plan=(user.plan or "starter"), context="worker_run"):
            _publish_log(r, job_id, "error", f"Plan limit — {_violation}", db=db)
            _fail_job(db, job, r, job_id, f"Plan limit reached: {_violation}")
            return
```

- [ ] **Step 2: Import smoke test**

Run: `python -c "import src.workers.tasks"`
Expected: clean import (no NameError; `_fail_job`, `select`, `ScraperConfig`, `User` already imported in this module — verify with `grep -n "from src.db.models" src/workers/tasks.py`).

- [ ] **Step 3: Commit**

```bash
git add src/workers/tasks.py
git commit -m "feat(entitlements): worker job-start backstop, audit mode (Phase 3)"
```

=== TASK 7 ===
### Task 7: Guard generic webhook send after downgrade

**Files:**
- Modify: `src/workers/tasks.py` (~line 1122, the `webhook_url` block)

- [ ] **Step 1: Wrap the webhook send** — change the guard at line 1122 from `if webhook_url and object_key:` to also require current plan (mirrors `dialer.py:118`):

```python
        from src.config.constants import BUSINESS_FEATURES_PLANS
        webhook_url = deliver_config.get("webhook_url")
        _wh_plan_ok = (user.plan or "starter").lower() in BUSINESS_FEATURES_PLANS
        if webhook_url and object_key and not _wh_plan_ok:
            _publish_log(r, job_id, "warning",
                         "Webhook delivery skipped — requires Business plan", db=db)
        if webhook_url and object_key and _wh_plan_ok:
```

(This recheck is unconditional — it is a feature gate, not the flagged county/type matrix — matching how `enrichment.skip_tracing`/dialer already behave. `user` is in scope from line 256.)

- [ ] **Step 2: Import smoke**

Run: `python -c "import src.workers.tasks"`
Expected: clean import.

- [ ] **Step 3: Commit**

```bash
git add src/workers/tasks.py
git commit -m "fix(entitlements): generic webhook rechecks current plan (Phase 3)"
```

