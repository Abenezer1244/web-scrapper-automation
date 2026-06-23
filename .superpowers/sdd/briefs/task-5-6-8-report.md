# Task 5-6-8 Report — Phase 3 Tier Enforcement Guards

## Task 5 — `src/workers/scheduler_helpers/dispatch.py`
Inserted the entitlement guard block at line 68, after the record-limit `skipped_limit += 1; continue` block, inside the `for config in configs:` loop. The guard locally imports `ConfigRow, config_run_violation, should_block_run` from `src.api.entitlements`, queries the active scraper configs for that user, calls `config_run_violation`, and calls `should_block_run` with `context="schedule_single"`. On violation with enforcement ON it `continue`s (skips the job). `select`, `ScraperConfig`, `User` were already imported (lines 25-27); `user` was loaded at line 59.

## Task 6 — `src/workers/batch_tasks.py`
Inserted the entitlement guard at the top of the `for c in configs:` loop (before `job = Job(...)`), at line 142. Same pattern: local import of the three entitlement helpers, active-config query scoped to `c.user_id`, `config_run_violation`, then `should_block_run` with `context="batch_fanout"`. On block → `continue` (child job skipped for that config). `select`, `ScraperConfig`, `User` were already top-level imports. `user` was loaded at line 119. Ruff auto-fixed import formatting (one-name-per-line inside the multi-line import).

## Task 8 — `src/api/auth.py`
- Added `from src.utils.logger import setup_logger` to the import block.
- Added `_logger = setup_logger("api.auth")` as a module-level constant (before the password hashing section).
- Inserted the plan check between the `if user_match is None: raise _CREDENTIALS_EXCEPTION` line and the `return AuthContext(...)` line. Uses a local import of `BUSINESS_FEATURES_PLANS` from `src.config.constants`. When enforcement is ON → raises `HTTPException(403, "API access requires a Business or Agency plan.")`. When OFF → `_logger.info(...)` audit line. Did NOT use the brief's `if user_match is not None:` wrapper (per correction: user_match is guaranteed non-None at that point).

## Test created: `tests/test_api_key_plan_guard.py`
Pure constant-membership test: asserts `"business"` and `"agency"` are in `BUSINESS_FEATURES_PLANS`, and `"pro"` and `"starter"` are not.

## Verification output

### Imports
```
imports OK
```
(All three modules imported cleanly with synthetic env.)

### Tests
```
10 passed in 41.15s
tests/test_api_key_plan_guard.py .
tests/test_entitlements_runtime.py .........
```

### Ruff
```
All checks passed!
```
(One I001 import-sort issue in batch_tasks.py was auto-fixed by `ruff --fix` before the final check.)

## Commit
`cf73876` — feat(entitlements): guard scheduler + batch fan-out + API-key use, audit mode (Phase 3)
4 files changed, 62 insertions(+)

## Concerns
- None material. The local import pattern (inside the loop) is intentional per the brief — avoids circular imports at module load time. Ruff accepted it after formatting.
- The batch fan-out guard uses `continue` which skips the child job but does NOT fail the run or reduce `child_job_ids` — if all children are blocked the run will still flip to "running" with an empty `enqueued` list, then the completion barrier will see zero children. This is consistent with how the existing over-limit path works and is audit-only until enforcement flips.

---

## Phase 3 Review Fix — `src/workers/batch_tasks.py` (stuck-run terminalization)

STATUS: DONE
Commit: `0ee676c` — fix(entitlements): terminalize batch run when all children blocked by plan limits (Phase 3 review)

Field name confirmation: `run.failed_children` and `run.completed_at` match exactly the records-limit branch at lines 124-126 — same attribute names, same structure (`[{"reason": "..."}]`).

Import OK: yes
Ruff: All checks passed!
