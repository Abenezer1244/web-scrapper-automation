# Task 4 + 7 Report — Worker Job-Start Backstop + Webhook Plan Recheck

## Task 4 Insertion (lines 257–274 after edit)

Inserted immediately after `user = db.execute(select(User).where(User.id == job.user_id)).scalar_one()` and before the `# ── QUEUED (atomic claim)` block:

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

All referenced names (`select`, `ScraperConfig`, `User`, `_fail_job`, `_publish_log`) confirmed in-scope via module-level imports at lines 193–196 and 46–57.

## Task 7 Insertion (lines 1132–1138 after edit)

Changed the existing `webhook_url = ...; if webhook_url and object_key:` block to:

```python
        from src.config.constants import BUSINESS_FEATURES_PLANS
        webhook_url = deliver_config.get("webhook_url")
        _wh_plan_ok = (user.plan or "starter").lower() in BUSINESS_FEATURES_PLANS
        if webhook_url and object_key and not _wh_plan_ok:
            _publish_log(r, job_id, "warning",
                         "Webhook delivery skipped — requires Business plan", db=db)
        if webhook_url and object_key and _wh_plan_ok:
```

`user` confirmed in scope from line 255. Unconditional gate (not behind `ENTITLEMENT_ENFORCEMENT` flag) — mirrors dialer recheck pattern.

## Verification Output

```
python -c "import src.workers.tasks; print('tasks import OK')"
tasks import OK

python -m ruff check src/workers/tasks.py
All checks passed!

python -m pytest tests/test_entitlements_runtime.py -q
9 passed in 38.72s
```

## Concerns

None. Both insertions match the brief exactly. The backstop runs BEFORE the atomic claim (`pending→queued` CAS), meaning a plan-blocked job is failed in `pending` state without ever being claimed — this is correct since `_fail_job` terminates it and `return` exits before the claim. The `from src.api.entitlements import ...` local import avoids circular-import risk at module level.

---

## Codex P1 Fix — Guard reordered to AFTER ownership CAS

STATUS: DONE
Commit: 52bca7c
Guard now after db.refresh(job)? yes
Exactly one copy? yes
Import OK? yes
