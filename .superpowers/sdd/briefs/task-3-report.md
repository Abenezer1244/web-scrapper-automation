# Task 3 Report — Guard POST /jobs at execution time (Phase 3)

## STATUS: COMPLETE

## Commit
`d19a3d2` — `feat(entitlements): guard POST /jobs at execution time, audit mode (Phase 3)`

## Files changed (3)
- `src/api/entitlements.py` — appended `enforce_runnable_http` and `should_block_run` helpers (+28 lines)
- `src/api/routes/jobs.py` — inserted execution-time guard block after `config is None` 404 check and before the AI-job-limit block (+16 lines); local import of `ConfigRow, config_run_violation, enforce_runnable_http` inside the route; selects `paused_reason` directly (column exists on model, migration 070 landed)
- `tests/test_jobs_entitlement_guard.py` — new pure-unit test asserting starter plan blocks `pre_foreclosure`

## Test output
```
10 passed in 41.07s
tests/test_jobs_entitlement_guard.py::test_guard_blocks_starter_preforeclosure PASSED
tests/test_entitlements_runtime.py::* (9 tests) PASSED
```

## Import smoke
```
imports OK
```

## Ruff
```
All checks passed!
```

## Concerns
- The brief's verbatim test included `import pytest` and `from src.config.settings import settings` — both unused. Ruff flagged them (F401, I001). Removed unused imports to keep ruff clean; test logic is identical to the brief.
- No other concerns. Guard is purely additive (audit-mode default); no behaviour change until `ENTITLEMENT_ENFORCEMENT=True`.
