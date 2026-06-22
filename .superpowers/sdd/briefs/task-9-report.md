# Task 9 Report — Per-user advisory lock (Phase 4 TOCTOU fix)

## What was done

### Change 1 — `src/api/routes/scrapers.py`
Inserted immediately before `await enforce_entitlements(` at line ~109 (now ~117 after insertion):

```python
    # TOCTOU fix (entitlements.py county-count race): serialize concurrent creates
    # for THIS user with a per-user advisory xact lock so the distinct-county count
    # in enforce_entitlements can't be raced. Auto-released at transaction end.
    # Namespaced (classid=4242 "entitlement") to avoid collision with other locks.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(4242, hashtext(:uid))"),
        {"uid": str(current_user.id)},
    )
```

`text` was already imported on line 7 — no import change needed.

### Change 2 — `src/api/routes/batches.py`
- Line 15: `from sqlalchemy import func, select` → `from sqlalchemy import func, select, text`
- Inserted the same 8-line advisory lock block immediately before `await enforce_entitlements(` at line ~129 (now ~137 after insertion).

### Change 3 — `tests/test_county_cap_race.py` (new file)
- `test_advisory_lock_sql_is_valid` — runs unconditionally, no DB needed; validates the SQL string is well-formed.
- `test_concurrent_creates_at_cap_both_cannot_pass` — decorated with `@pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", ...)`, skipped by default; placeholder for Phase 7 live verification.

## Verification output

### imports OK
```
imports OK
```

### pytest output
```
tests/test_county_cap_race.py::test_concurrent_creates_at_cap_both_cannot_pass SKIPPED
tests/test_county_cap_race.py::test_advisory_lock_sql_is_valid PASSED
======================== 1 passed, 1 skipped in 4.21s =========================
```

### ruff output
```
All checks passed!
```

## Concerns
None. The advisory lock is a no-op in the audit-only path (ENTITLEMENT_ENFORCEMENT=off) — it serializes creates per user but adds no round-trip beyond the single `SELECT` which Postgres executes instantly for non-contended locks.
