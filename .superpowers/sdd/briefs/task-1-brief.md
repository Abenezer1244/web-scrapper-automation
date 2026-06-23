### Task 1: Correct stale plan-catalog copy

**Files:**
- Modify: `src/api/routes/billing.py:235-303` (`_PLANS`)
- Test: `tests/test_billing_catalog_matches_matrix.py` (create)

**Interfaces:**
- Consumes: `RECORD_TYPES_BY_PLAN`, `COUNTY_LIMIT_BY_PLAN` from `src/config/constants.py`.
- Produces: nothing new (copy + a guard test).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_catalog_matches_matrix.py
"""The customer-facing plan catalog must not promise access the matrix denies."""
from src.api.routes.billing import _PLANS
from src.config.constants import COUNTY_LIMIT_BY_PLAN, RECORD_TYPES_BY_PLAN


def _plan(pid: str) -> dict:
    return next(p for p in _PLANS if p["id"] == pid)


def test_pro_does_not_claim_all_record_types():
    feats = " ".join(_plan("pro")["features"]).lower()
    # Pro is the 3 core lists, NOT all types.
    assert "all record types" not in feats
    assert len(RECORD_TYPES_BY_PLAN["pro"]) == 3


def test_pro_advertises_its_three_county_cap():
    feats = " ".join(_plan("pro")["features"]).lower()
    assert "3 counties" in feats
    assert COUNTY_LIMIT_BY_PLAN["pro"] == 3


def test_business_advertises_all_types_and_ten_counties():
    feats = " ".join(_plan("business")["features"]).lower()
    assert "all record types" in feats
    assert "10 counties" in feats
    assert COUNTY_LIMIT_BY_PLAN["business"] == 10
    assert RECORD_TYPES_BY_PLAN["business"] == RECORD_TYPES_BY_PLAN["agency"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_billing_catalog_matches_matrix.py -v`
Expected: FAIL (`test_pro_does_not_claim_all_record_types` — Pro currently lists "All record types"; `test_pro_advertises_its_three_county_cap` — no county bullet).

- [ ] **Step 3: Fix the Pro feature bullets**

In `src/api/routes/billing.py`, replace the Pro `features` list (currently lines 253-261) and update the stale comment:

```python
        # Bullets describe ENFORCED entitlements only (value-metric build,
        # docs/pricing-strategy-2026-06.md §4): Pro = 3 counties + the 3 core
        # distress lists. Premium lists + overlap are a Business feature.
        "features": [
            "1,000 records/month",
            "3 counties (your choice)",
            "Probate, pre-foreclosure & tax-delinquent lists",
            "Skip tracing (250 included, then $0.08/lookup)",
            "CSV + Excel export",
            "Daily/weekly schedule",
            "Email delivery",
            "Batch scraping",
        ],
```

- [ ] **Step 4: Fix the Business feature bullets**

Replace the Business `features` list (currently lines 272-281):

```python
        "features": [
            "5,000 records/month",
            "10 counties (your choice)",
            "All record types + overlap/intersection",
            "All export formats",
            "All schedules",
            "Email + Webhook + dialer delivery",
            "Skip tracing (1,000 included)",
            "API access",
        ],
```

Note: the "5 team members" bullet is removed — seat enforcement does not exist (spec §8). Leave Agency's "Unlimited team members" removed too if present; replace Agency's first lines to drop seats:

```python
        "features": [
            "Unlimited counties + records",
            "All record types + overlap/intersection",
            "Skip tracing (2,000 included)",
            "White-label (coming soon)",
            "Priority queue + support",
            "Dedicated account manager",
        ],
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_billing_catalog_matches_matrix.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/billing.py tests/test_billing_catalog_matches_matrix.py
git commit -m "fix(billing): plan catalog copy matches enforced tier matrix (Phase 1)"
```

---


## GLOBAL CONSTRAINTS (bind this task)

- Canonical matrix = `docs/pricing-strategy-2026-06.md` §4 = `src/config/constants.py` (`COUNTY_LIMIT_BY_PLAN:149`, `RECORD_TYPES_BY_PLAN:170`). Do NOT change those values.
- Counties = count-based, user-choice. Record types = capability menu (Starter `probate`; Pro `probate`/`pre_foreclosure`/`tax_delinquent`; Business+Agency all 6).
- All new checks gated by `settings.ENTITLEMENT_ENFORCEMENT` (default False): OFF = log audit + proceed; ON = block (402 API / fail job / skip dispatch).
- Fail CLOSED on unknown plan = `starter` (existing `entitlements.py` convention).
- No mocks/dummies; tests use real DB + real settings (`.claude/rules/testing.md`). `_norm_county(state, county)` is the single normalization helper — reuse it.
- Multi-tenant: every query filters by `user_id` (RLS belt + filter suspenders).
- Each phase ≤5 files; complete + verify a phase, then get explicit user approval before the next (CLAUDE.md). Codex reviews every phase diff; any Critical/High = NO-GO until fixed.
- Errors to clients carry no stack trace/raw DB error.

