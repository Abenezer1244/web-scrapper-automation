# Task 1 Report — Plan Catalog Copy Correction

**Status:** DONE  
**Commit:** `108f373`  
**Branch:** `feat/purge-followup-fk-index-migration` (worktree `tier-enforcement`)

---

## What Changed

### `src/api/routes/billing.py` — `_PLANS` feature bullets

**Pro plan** (`lines 250-261` before, `lines 250-262` after):
- Removed misleading "All record types" bullet (Pro is gated to 3 types)
- Added "3 counties (your choice)" to match `COUNTY_LIMIT_BY_PLAN["pro"] == 3`
- Changed "Probate, pre-foreclosure & tax-delinquent lists" for specificity
- Updated skip-tracing bullet to match enforced entitlement (250 included, $0.08/lookup)
- Updated stale comment to reference `docs/pricing-strategy-2026-06.md §4`

**Business plan** (`lines 272-281` before, `lines 272-280` after):
- Added "10 counties (your choice)" to match `COUNTY_LIMIT_BY_PLAN["business"] == 10`
- Changed "All record types" → "All record types + overlap/intersection"
- Removed "5 team members" (seat enforcement does not exist per spec §8)
- Updated delivery bullet to add dialer

**Agency plan** (`lines 291-298` before, `lines 291-297` after):
- Changed "Unlimited records" → "Unlimited counties + records"
- Removed "All features" (too vague/misleading)
- Changed "All features" → "All record types + overlap/intersection"
- Removed "Unlimited team members" (seat enforcement does not exist per spec §8)
- Changed "Priority support" → "Priority queue + support"

### `tests/test_billing_catalog_matches_matrix.py` — new file

Pure unit test (no DB/redis), 3 assertions:
1. `test_pro_does_not_claim_all_record_types` — Pro catalog must not say "all record types"; `RECORD_TYPES_BY_PLAN["pro"]` must have exactly 3 types
2. `test_pro_advertises_its_three_county_cap` — Pro catalog must contain "3 counties"; `COUNTY_LIMIT_BY_PLAN["pro"]` must == 3
3. `test_business_advertises_all_types_and_ten_counties` — Business catalog must contain "all record types" and "10 counties"; matrix values validated

---

## Test Output (verbatim)

### Before edits (confirms initial failure):
```
============================= test session starts =============================
collected 3 items

tests/test_billing_catalog_matches_matrix.py::test_pro_does_not_claim_all_record_types FAILED [ 33%]
tests/test_billing_catalog_matches_matrix.py::test_pro_advertises_its_three_county_cap FAILED [ 66%]
tests/test_billing_catalog_matches_matrix.py::test_business_advertises_all_types_and_ten_counties FAILED [100%]

=========================== 3 failed in 13.38s ==============================
```

### After edits (all pass):
```
============================= test session starts =============================
collected 3 items

tests/test_billing_catalog_matches_matrix.py::test_pro_does_not_claim_all_record_types PASSED [ 33%]
tests/test_billing_catalog_matches_matrix.py::test_pro_advertises_its_three_county_cap PASSED [ 66%]
tests/test_billing_catalog_matches_matrix.py::test_business_advertises_all_types_and_ten_counties PASSED [100%]

=========================== 3 passed in 12.78s ==============================
```

---

## Ruff Output

```
All checks passed!
```

(Run on `src/api/routes/billing.py` and `tests/test_billing_catalog_matches_matrix.py`)

---

## Constraints Verified

- `src/config/constants.py` — NOT touched. Values unchanged.
- `_PRICE_TO_PLAN` map — NOT touched. `records_limit` and `id` values unchanged.
- Only 2 files modified: `src/api/routes/billing.py` and `tests/test_billing_catalog_matches_matrix.py`.
- Test is pure unit (no DB/redis/client fixtures); passes under synthetic test env.

---

## Concerns

None. Changes are purely cosmetic copy corrections to feature bullet strings. No logic, no schema, no Stripe mapping touched.
