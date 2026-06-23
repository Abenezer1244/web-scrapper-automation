# Task 2 Report: Runtime entitlement helpers

## Files changed
- `src/api/entitlements.py` — appended `ConfigRow`, `_candidate_rows`, `allowed_county_set`, `config_run_violation`, `plan_reconciliation`, `PAUSED_REASON_ENTITLEMENT`; moved `dataclasses` + `datetime` imports to top of file to satisfy E402.
- `tests/test_entitlements_runtime.py` — created with 8 TDD tests; ruff auto-fixed import sort order (I001).

## Red → Green (verbatim)

### Red (before implementation)
```
ERROR tests/test_entitlements_runtime.py
ImportError: cannot import name 'ConfigRow' from 'src.api.entitlements'
1 error during collection
```

### Green (after implementation)
```
collected 8 items
tests/test_entitlements_runtime.py::test_allowed_county_set_keeps_oldest_n PASSED
tests/test_entitlements_runtime.py::test_allowed_county_set_unlimited_returns_none PASSED
tests/test_entitlements_runtime.py::test_run_violation_blocks_disallowed_record_type PASSED
tests/test_entitlements_runtime.py::test_run_violation_blocks_county_over_cap PASSED
tests/test_entitlements_runtime.py::test_run_violation_passes_within_plan PASSED
tests/test_entitlements_runtime.py::test_reconciliation_pauses_over_limit_and_revives PASSED
tests/test_entitlements_runtime.py::test_reconciliation_revives_previously_paused_on_upgrade PASSED
tests/test_entitlements_runtime.py::test_reconciliation_ignores_user_paused_configs PASSED
8 passed in 32.88s
```

## Ruff output
```
All checks passed!
```
(2 E402 errors fixed by moving stdlib imports to top; 1 I001 auto-fixed via `--fix` on test file)

## Concerns
None. Implementation is verbatim from the brief. Pure functions, no DB/IO, reuses existing `_norm_county`, `COUNTY_LIMIT_BY_PLAN`, `RECORD_TYPES_BY_PLAN`.

---
STATUS: DONE
Commit: 37fd3a0
Tests: 8 passed

---

# Codex P2 Fix Report: active-before-paused slot ordering

## What changed
- `src/api/entitlements.py` — replaced `allowed_county_set` body: split candidate pool into `active_earliest` and `paused_earliest` dicts; active counties claim slots first (sorted by created_at, then key); entitlement-paused counties fill only remaining slots. Deleted `_candidate_rows` (dead code: nothing called it after the rewrite).
- `tests/test_entitlements_runtime.py` — added `test_active_county_not_evicted_by_older_paused` (cap=1 starter; King paused older, Pierce active newer → Pierce keeps slot; reconciliation: no pause, no revive).

## Test output (verbatim)
```
collected 9 items

tests/test_entitlements_runtime.py::test_allowed_county_set_keeps_oldest_n PASSED
tests/test_entitlements_runtime.py::test_allowed_county_set_unlimited_returns_none PASSED
tests/test_entitlements_runtime.py::test_run_violation_blocks_disallowed_record_type PASSED
tests/test_entitlements_runtime.py::test_run_violation_blocks_county_over_cap PASSED
tests/test_entitlements_runtime.py::test_run_violation_passes_within_plan PASSED
tests/test_entitlements_runtime.py::test_reconciliation_pauses_over_limit_and_revives PASSED
tests/test_entitlements_runtime.py::test_reconciliation_revives_previously_paused_on_upgrade PASSED
tests/test_entitlements_runtime.py::test_reconciliation_ignores_user_paused_configs PASSED
tests/test_entitlements_runtime.py::test_active_county_not_evicted_by_older_paused PASSED

9 passed in 38.58s
```

## Ruff output
```
All checks passed!
```

## _candidate_rows disposition
DELETED. After the rewrite, `allowed_county_set` iterates `rows` directly with two separate dicts. `_candidate_rows` had no other callers in the production code (only referenced in docs/briefs).

---
STATUS: DONE
Commit: e9cd3a6
Tests: 9 passed
