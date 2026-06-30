# Tier Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each subscription tier's access/limits an *execution-time invariant* so Starter/Pro/Business/Agency limits are actually enforced everywhere a scrape can run or data can leave the system — then flip enforcement on safely.

**Architecture:** Add pure, unit-tested entitlement helpers (`allowed_county_set`, `config_run_violation`, `plan_reconciliation`) to `src/api/entitlements.py`. Wire `config_run_violation` into all six leak points (POST /jobs, worker, scheduler, batch fan-out, generic webhook, API-key auth) in audit mode behind the existing `ENTITLEMENT_ENFORCEMENT` flag. Add downgrade reconciliation (pauses over-limit configs with a new `paused_reason` column) on Stripe downgrade + trial-expiry. Lock the user row in create paths to close the county-count race. Measure from audit logs, then flip the flag.

**Tech Stack:** FastAPI (async), Celery (sync `SyncSessionLocal`), SQLAlchemy, Alembic, PostgreSQL (Supabase RLS), pytest.

## Global Constraints

- Canonical matrix = `docs/pricing-strategy-2026-06.md` §4 = `src/config/constants.py` (`COUNTY_LIMIT_BY_PLAN:149`, `RECORD_TYPES_BY_PLAN:170`). Do NOT change those values.
- Counties = count-based, user-choice. Record types = capability menu (Starter `probate`; Pro `probate`/`pre_foreclosure`/`tax_delinquent`; Business+Agency all 6).
- All new checks gated by `settings.ENTITLEMENT_ENFORCEMENT` (default False): OFF = log audit + proceed; ON = block (402 API / fail job / skip dispatch).
- Fail CLOSED on unknown plan = `starter` (existing `entitlements.py` convention).
- No mocks/dummies; tests use real DB + real settings (`.claude/rules/testing.md`). `_norm_county(state, county)` is the single normalization helper — reuse it.
- Multi-tenant: every query filters by `user_id` (RLS belt + filter suspenders).
- Each phase ≤5 files; complete + verify a phase, then get explicit user approval before the next (CLAUDE.md). Codex reviews every phase diff; any Critical/High = NO-GO until fixed.
- Errors to clients carry no stack trace/raw DB error.

---

## File map

- **Modify** `src/api/entitlements.py` — add pure helpers + two action wrappers (Task 2).
- **Test** `tests/test_entitlements_runtime.py` — new (Task 2).
- **Modify** `src/api/routes/billing.py:235-303` — fix stale `_PLANS` copy (Task 1).
- **Test** `tests/test_billing_catalog_matches_matrix.py` — new (Task 1).
- **Modify** `src/api/routes/jobs.py:79` — POST /jobs guard (Task 3).
- **Modify** `src/workers/tasks.py:256`, `:1122` — worker guard + webhook guard (Tasks 4, 7).
- **Modify** `src/workers/scheduler_helpers/dispatch.py:59`, batch path — dispatch guards (Tasks 5, 6).
- **Modify** `src/workers/batch_tasks.py:141` — fan-out guard (Task 6).
- **Modify** `src/api/auth.py:298` — API-key plan guard (Task 8).
- **Modify** `src/api/routes/scrapers.py:109`, `src/api/routes/batches.py:129` — user-row lock (Task 9).
- **Create** `alembic/versions/070_*.py` — `paused_reason` column (Task 10).
- **Modify** `src/db/models.py:236` — model column (Task 10).
- **Modify** `src/api/routes/billing.py:814,828`, `src/workers/scheduler_helpers/billing.py:66` — reconciliation hooks (Task 11).
- **Create** `scripts/audit_entitlement_violations.py` — measurement (Task 12).

---

# PHASE 1 — Freeze the product matrix

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

# PHASE 2 — Core entitlement logic (pure, unit-tested)

### Task 2: Runtime entitlement helpers

**Files:**
- Modify: `src/api/entitlements.py` (append helpers)
- Test: `tests/test_entitlements_runtime.py` (create)

**Interfaces:**
- Consumes: `COUNTY_LIMIT_BY_PLAN`, `RECORD_TYPES_BY_PLAN`, existing `_norm_county`, `_plan_of`.
- Produces (used by Tasks 3-8, 11):
  - `@dataclass(frozen=True) class ConfigRow` with fields `id: str, state: str, county: str, record_type: str, created_at: datetime, active: bool, paused_reason: str | None`.
  - `allowed_county_set(rows: Iterable[ConfigRow], plan: str) -> set[tuple[str, str]] | None` — `None` = unlimited.
  - `config_run_violation(plan: str, state: str, county: str, record_type: str, active_rows: Iterable[ConfigRow]) -> str | None`.
  - `plan_reconciliation(rows: Iterable[ConfigRow], plan: str) -> tuple[set[str], set[str]]` → `(pause_ids, revive_ids)`.
  - `PAUSED_REASON_ENTITLEMENT = "entitlement"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entitlements_runtime.py
from datetime import UTC, datetime, timedelta

from src.api.entitlements import (
    ConfigRow,
    allowed_county_set,
    config_run_violation,
    plan_reconciliation,
    PAUSED_REASON_ENTITLEMENT,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _row(i, county, rt="probate", mins=0, active=True, paused=None, state="WA"):
    return ConfigRow(
        id=str(i), state=state, county=county, record_type=rt,
        created_at=_T0 + timedelta(minutes=mins), active=active, paused_reason=paused,
    )


def test_allowed_county_set_keeps_oldest_n():
    rows = [_row(1, "King", mins=0), _row(2, "Pierce", mins=1), _row(3, "Snohomish", mins=2)]
    # pro cap = 3 → all allowed
    assert allowed_county_set(rows, "pro") == {("WA", "king"), ("WA", "pierce"), ("WA", "snohomish")}
    # starter cap = 1 → oldest only
    assert allowed_county_set(rows, "starter") == {("WA", "king")}


def test_allowed_county_set_unlimited_returns_none():
    assert allowed_county_set([_row(1, "King")], "agency") is None


def test_run_violation_blocks_disallowed_record_type():
    rows = [_row(1, "King", rt="pre_foreclosure")]
    v = config_run_violation("starter", "WA", "King", "pre_foreclosure", rows)
    assert v is not None and "record type" in v


def test_run_violation_blocks_county_over_cap():
    rows = [_row(1, "King", mins=0), _row(2, "Pierce", mins=1)]
    # starter cap 1 → Pierce (newer) blocked, King allowed
    assert config_run_violation("starter", "WA", "Pierce", "probate", rows) is not None
    assert config_run_violation("starter", "WA", "King", "probate", rows) is None


def test_run_violation_passes_within_plan():
    rows = [_row(1, "King", rt="probate")]
    assert config_run_violation("pro", "WA", "King", "probate", rows) is None


def test_reconciliation_pauses_over_limit_and_revives():
    # Business user with 2 counties + a premium type, downgraded to pro
    rows = [
        _row(1, "King", rt="probate", mins=0),
        _row(2, "Pierce", rt="divorce", mins=1),   # premium type → not in pro
        _row(3, "Snohomish", rt="tax_delinquent", mins=2),
        _row(4, "Clark", rt="probate", mins=3),    # 4th county → over pro cap of 3
    ]
    pause, revive = plan_reconciliation(rows, "pro")
    assert pause == {"2", "4"}   # premium type + 4th county
    assert revive == set()


def test_reconciliation_revives_previously_paused_on_upgrade():
    rows = [
        _row(1, "King", rt="probate", mins=0, active=True),
        _row(2, "Pierce", rt="divorce", mins=1, active=False, paused=PAUSED_REASON_ENTITLEMENT),
    ]
    # upgraded to business → divorce now allowed, county within cap → revive #2
    pause, revive = plan_reconciliation(rows, "business")
    assert pause == set()
    assert revive == {"2"}


def test_reconciliation_ignores_user_paused_configs():
    rows = [_row(1, "King", rt="probate", active=False, paused=None)]  # user-paused
    pause, revive = plan_reconciliation(rows, "starter")
    assert pause == set() and revive == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_entitlements_runtime.py -v`
Expected: FAIL with `ImportError: cannot import name 'ConfigRow'`.

- [ ] **Step 3: Append the implementation to `src/api/entitlements.py`**

```python
# ── Runtime (execution-time) entitlement helpers ─────────────────────────────
from dataclasses import dataclass
from datetime import datetime

PAUSED_REASON_ENTITLEMENT = "entitlement"


@dataclass(frozen=True)
class ConfigRow:
    """Minimal projection of a ScraperConfig for entitlement math. Decoupled from
    the ORM so the logic is pure and unit-testable."""
    id: str
    state: str
    county: str
    record_type: str
    created_at: datetime
    active: bool = True
    paused_reason: str | None = None


def _candidate_rows(rows: Iterable[ConfigRow]) -> list[ConfigRow]:
    """Configs that may legitimately hold a county slot: currently active OR
    previously paused BY entitlement (revival candidates). User-paused configs
    (active=False, paused_reason None) are excluded — the user chose to stop them."""
    return [
        r for r in rows
        if r.active or r.paused_reason == PAUSED_REASON_ENTITLEMENT
    ]


def allowed_county_set(
    rows: Iterable[ConfigRow], plan: str
) -> set[tuple[str, str]] | None:
    """Normalized (STATE, county) jurisdictions the plan permits, chosen
    deterministically as the earliest-created `cap` distinct counties. Returns
    None for unlimited plans (cap < 0)."""
    plan = (plan or "starter").lower()
    cap = COUNTY_LIMIT_BY_PLAN.get(plan, COUNTY_LIMIT_BY_PLAN["starter"])
    if cap < 0:
        return None
    earliest: dict[tuple[str, str], datetime] = {}
    for row in _candidate_rows(rows):
        key = _norm_county(row.state, row.county)
        if key not in earliest or row.created_at < earliest[key]:
            earliest[key] = row.created_at
    # Sort by (created_at, key) so ties are deterministic.
    ranked = sorted(earliest.items(), key=lambda kv: (kv[1], kv[0]))
    return {key for key, _ in ranked[:cap]}


def config_run_violation(
    plan: str,
    state: str,
    county: str,
    record_type: str,
    active_rows: Iterable[ConfigRow],
) -> str | None:
    """Return a human-readable reason if running this (county, record_type) is NOT
    permitted under the user's CURRENT plan, else None. Fails closed on unknown plan."""
    plan = (plan or "starter").lower()
    rt = (record_type or "").lower()
    allowed_types = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])
    if rt not in allowed_types:
        return (
            f"record type '{rt}' is not in your '{plan}' plan "
            f"(allowed: {sorted(allowed_types)})"
        )
    allowed = allowed_county_set(active_rows, plan)
    if allowed is not None:
        key = _norm_county(state, county)
        if key not in allowed:
            cap = COUNTY_LIMIT_BY_PLAN.get(plan, COUNTY_LIMIT_BY_PLAN["starter"])
            return (
                f"county {key[1]}, {key[0]} is outside your '{plan}' plan's "
                f"{cap}-county limit"
            )
    return None


def plan_reconciliation(
    rows: Iterable[ConfigRow], plan: str
) -> tuple[set[str], set[str]]:
    """Given ALL of a user's configs, return (pause_ids, revive_ids) for a plan.

    pause_ids  = currently-active configs no longer permitted under `plan`.
    revive_ids = entitlement-paused configs now permitted again.
    User-paused configs (paused_reason None, active False) are never touched."""
    rows = list(rows)
    plan = (plan or "starter").lower()
    allowed_counties = allowed_county_set(rows, plan)
    allowed_types = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])

    def _permitted(r: ConfigRow) -> bool:
        if r.record_type.lower() not in allowed_types:
            return False
        if allowed_counties is None:
            return True
        return _norm_county(r.state, r.county) in allowed_counties

    pause_ids: set[str] = set()
    revive_ids: set[str] = set()
    for r in rows:
        if r.active and not _permitted(r):
            pause_ids.add(r.id)
        elif (not r.active) and r.paused_reason == PAUSED_REASON_ENTITLEMENT and _permitted(r):
            revive_ids.add(r.id)
    return pause_ids, revive_ids
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_entitlements_runtime.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Type-check + lint**

Run: `python -m mypy src/api/entitlements.py 2>/dev/null || true` then `ruff check src/api/entitlements.py tests/test_entitlements_runtime.py`
Expected: ruff clean (mypy optional if not configured — state which).

- [ ] **Step 6: Commit**

```bash
git add src/api/entitlements.py tests/test_entitlements_runtime.py
git commit -m "feat(entitlements): execution-time guard + reconciliation helpers (Phase 2)"
```

---

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

**PHASE 3 GATE:** `pytest tests/ -k "entitlement or billing_catalog" -v` all green; `ruff check src tests` clean; Codex `codex review --base <base>`. Get user approval before Phase 4.

---

# PHASE 4 — Close the county-count race (TOCTOU)

### Task 9: Lock the user row in create paths

**Files:**
- Modify: `src/api/routes/scrapers.py` (before `enforce_entitlements` at line 109)
- Modify: `src/api/routes/batches.py` (before `enforce_entitlements` at line 129)

**Interfaces:**
- Consumes: existing `enforce_entitlements`; adds a row lock so concurrent creates serialize.

- [ ] **Step 1: Add the lock in `src/api/routes/scrapers.py`** immediately before the `await enforce_entitlements(` call (line 109):

```python
    # TOCTOU fix: serialize concurrent creates for this user so the distinct-county
    # count in enforce_entitlements can't be raced (entitlements.py:78-81). The lock
    # is released at request-transaction end. Harmless when enforcement is OFF.
    await db.execute(
        select(User.id).where(User.id == current_user.id).with_for_update()
    )
```

(`User` and `select` are already imported in `scrapers.py` — verify with `grep -n "from src.db.models" src/api/routes/scrapers.py`; add `User` to the import if absent.)

- [ ] **Step 2: Add the same lock in `src/api/routes/batches.py`** before line 129's `enforce_entitlements`:

```python
    await db.execute(
        select(User.id).where(User.id == current_user.id).with_for_update()
    )
```

- [ ] **Step 3: Write the concurrency test**

```python
# tests/test_county_cap_race.py
"""Two concurrent creates at the cap must not both succeed when enforcement is ON.
Requires a real DB; skipped if DATABASE_URL is unset."""
import os
import asyncio
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DB")


@pytest.mark.asyncio
async def test_concurrent_creates_serialize(monkeypatch):
    from src.config.settings import settings
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", True)
    # Arrange: a starter user (cap 1) with 0 configs, fire two create requests for
    # two different counties concurrently; exactly one should 402.
    # (Use the project's existing async test client + user factory.)
    ...
```

(Flesh out using the repo's existing async client/user fixtures — see `tests/` for the established pattern. The assertion: exactly one of the two concurrent creates returns 402.)

- [ ] **Step 4: Run + import smoke**

Run: `python -c "import src.api.routes.scrapers, src.api.routes.batches"` then `pytest tests/test_county_cap_race.py -v` (will skip without DB; run against the local dev DB to confirm).

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/scrapers.py src/api/routes/batches.py tests/test_county_cap_race.py
git commit -m "fix(entitlements): lock user row before county-count check, close TOCTOU (Phase 4)"
```

**PHASE 4 GATE:** import smoke green; Codex `codex challenge` on the locking (race/deadlock focus). User approval before Phase 5.

---

# PHASE 5 — Downgrade reconciliation

### Task 10: `paused_reason` column

**Files:**
- Create: `alembic/versions/070_scraper_config_paused_reason.py`
- Modify: `src/db/models.py:236` (add column)

- [ ] **Step 1: Add the model column** in `ScraperConfig` after `active` (line 237):

```python
    # Why a config is inactive. NULL = active or user-paused; 'entitlement' =
    # auto-paused by downgrade reconciliation (revived on re-upgrade). Distinct
    # from `active` so re-upgrade only revives what the system paused.
    paused_reason = Column(String(32), nullable=True)
```

- [ ] **Step 2: Create the migration** (revises 069):

```python
# alembic/versions/070_scraper_config_paused_reason.py
"""scraper_configs.paused_reason for entitlement reconciliation

Revision ID: 070_paused_reason
Revises: 069_index_unindexed_fks
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "070_paused_reason"
down_revision = "069_index_unindexed_fks"  # VERIFY against `alembic heads`
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scraper_configs",
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scraper_configs", "paused_reason")
```

- [ ] **Step 3: Verify the down_revision** matches the current head:

Run: `python -m alembic heads` (or the project's alembic invocation)
Expected: a single head; set `down_revision` to that exact id. Fix if `069_index_unindexed_fks` is not the literal id.

- [ ] **Step 4: Apply + roundtrip on the dev DB**

Run: `python -m alembic upgrade head` then `python -c "import src.db.models"`
Expected: column added; clean import.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/070_scraper_config_paused_reason.py src/db/models.py
git commit -m "feat(db): migration 070 — scraper_configs.paused_reason (Phase 5)"
```

### Task 11: Reconciliation wrappers + hooks

**Files:**
- Modify: `src/api/entitlements.py` (add async + sync apply wrappers)
- Modify: `src/api/routes/billing.py:814,828` (call async wrapper after plan change)
- Modify: `src/workers/scheduler_helpers/billing.py:66` (call sync wrapper after trial downgrade)
- Test: `tests/test_reconciliation_apply.py` (create, real DB)

**Interfaces:**
- Consumes: `plan_reconciliation`, `ConfigRow`, `PAUSED_REASON_ENTITLEMENT`.
- Produces:
  - `async def apply_reconciliation_async(db: AsyncSession, user_id: str, plan: str) -> tuple[int, int]`
  - `def apply_reconciliation_sync(db: Session, user_id: str, plan: str) -> tuple[int, int]`
  - both return `(paused_count, revived_count)`.

- [ ] **Step 1: Append the apply wrappers to `src/api/entitlements.py`**

```python
def _load_all_rows_sync(db, user_id: str) -> list[ConfigRow]:
    from src.db.models import ScraperConfig
    from sqlalchemy import select
    res = db.execute(
        select(
            ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
            ScraperConfig.record_type, ScraperConfig.created_at,
            ScraperConfig.active, ScraperConfig.paused_reason,
        ).where(ScraperConfig.user_id == user_id)
    ).all()
    return [ConfigRow(*r) for r in res]


def apply_reconciliation_sync(db, user_id: str, plan: str) -> tuple[int, int]:
    """Pause now-over-limit configs and revive entitlement-paused ones for `plan`.
    Caller commits. Idempotent: re-running yields (0, 0)."""
    from src.db.models import ScraperConfig
    from sqlalchemy import update
    rows = _load_all_rows_sync(db, user_id)
    pause_ids, revive_ids = plan_reconciliation(rows, plan)
    if pause_ids:
        db.execute(
            update(ScraperConfig)
            .where(ScraperConfig.id.in_(pause_ids))
            .values(active=False, paused_reason=PAUSED_REASON_ENTITLEMENT)
        )
    if revive_ids:
        db.execute(
            update(ScraperConfig)
            .where(ScraperConfig.id.in_(revive_ids))
            .values(active=True, paused_reason=None)
        )
    if pause_ids or revive_ids:
        _logger.info("reconcile user=%s plan=%s paused=%d revived=%d",
                     user_id, plan, len(pause_ids), len(revive_ids))
    return len(pause_ids), len(revive_ids)


async def apply_reconciliation_async(db, user_id: str, plan: str) -> tuple[int, int]:
    """Async twin of apply_reconciliation_sync for the Stripe webhook path."""
    from src.db.models import ScraperConfig
    from sqlalchemy import select, update
    res = await db.execute(
        select(
            ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
            ScraperConfig.record_type, ScraperConfig.created_at,
            ScraperConfig.active, ScraperConfig.paused_reason,
        ).where(ScraperConfig.user_id == user_id)
    )
    rows = [ConfigRow(*r) for r in res.all()]
    pause_ids, revive_ids = plan_reconciliation(rows, plan)
    if pause_ids:
        await db.execute(
            update(ScraperConfig).where(ScraperConfig.id.in_(pause_ids))
            .values(active=False, paused_reason=PAUSED_REASON_ENTITLEMENT)
        )
    if revive_ids:
        await db.execute(
            update(ScraperConfig).where(ScraperConfig.id.in_(revive_ids))
            .values(active=True, paused_reason=None)
        )
    if pause_ids or revive_ids:
        _logger.info("reconcile(async) user=%s plan=%s paused=%d revived=%d",
                     user_id, plan, len(pause_ids), len(revive_ids))
    return len(pause_ids), len(revive_ids)
```

- [ ] **Step 2: Write the failing real-DB test**

```python
# tests/test_reconciliation_apply.py
import os
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DB")


def test_downgrade_pauses_then_upgrade_revives(db_session, user_factory, config_factory):
    # Business user, 2 counties + a divorce config
    user = user_factory(plan="business")
    config_factory(user, county="King", record_type="probate")
    c_div = config_factory(user, county="Pierce", record_type="divorce")
    from src.api.entitlements import apply_reconciliation_sync, PAUSED_REASON_ENTITLEMENT
    paused, revived = apply_reconciliation_sync(db_session, user.id, "pro")
    db_session.commit()
    db_session.refresh(c_div)
    assert paused == 1 and revived == 0
    assert c_div.active is False and c_div.paused_reason == PAUSED_REASON_ENTITLEMENT
    # upgrade back
    paused2, revived2 = apply_reconciliation_sync(db_session, user.id, "business")
    db_session.commit()
    db_session.refresh(c_div)
    assert revived2 == 1 and c_div.active is True and c_div.paused_reason is None
```

(Use the repo's actual fixtures; if none exist for users/configs, build minimal real rows in the test and clean them up — no mocks.)

- [ ] **Step 3: Run test (against dev DB)**

Run: `pytest tests/test_reconciliation_apply.py -v`
Expected: PASS.

- [ ] **Step 4: Hook into the Stripe webhook handlers** in `src/api/routes/billing.py`:

In `_handle_subscription_updated` after `await db.flush()` (line 816) and in `_handle_subscription_deleted` after `await db.flush()` (line 830), add:

```python
        from src.api.entitlements import apply_reconciliation_async
        await apply_reconciliation_async(db, str(user.id), user.plan)
```

- [ ] **Step 5: Hook into trial-expiry** in `src/workers/scheduler_helpers/billing.py` inside the `for user in expired:` loop after `user.records_limit = settings.PLAN_LIMITS["starter"]`:

```python
                from src.api.entitlements import apply_reconciliation_sync
                apply_reconciliation_sync(db, str(user.id), "starter")
```

(Confirm the loop commits afterward — the existing `_expire_trials_impl` commits at function end; reconciliation rides that commit.)

- [ ] **Step 6: Import smoke + test**

Run: `python -c "import src.api.routes.billing, src.workers.scheduler_helpers.billing" && pytest tests/test_reconciliation_apply.py -v`
Expected: clean import + PASS.

- [ ] **Step 7: Commit**

```bash
git add src/api/entitlements.py src/api/routes/billing.py src/workers/scheduler_helpers/billing.py tests/test_reconciliation_apply.py
git commit -m "feat(entitlements): downgrade reconciliation on Stripe + trial expiry (Phase 5)"
```

**PHASE 5 GATE:** full `pytest tests/ -k entitlement or reconciliation` green; Codex `codex review`. User approval before Phase 6.

---

# PHASE 6 — Measure, then flip

### Task 12: Audit-violation report + flip

**Files:**
- Create: `scripts/audit_entitlement_violations.py`

- [ ] **Step 1: Write the measurement script** (read-only; lists who would be blocked under each tier's matrix):

```python
# scripts/audit_entitlement_violations.py
"""Read-only: enumerate users whose ACTIVE configs would be blocked once
ENTITLEMENT_ENFORCEMENT flips. Run against prod DATABASE_URL before flipping.

Usage: python scripts/audit_entitlement_violations.py
"""
from collections import defaultdict
from sqlalchemy import select
from src.db.session import SyncSessionLocal
from src.db.models import ScraperConfig, User
from src.api.entitlements import ConfigRow, plan_reconciliation


def main() -> None:
    with SyncSessionLocal() as db:
        users = db.execute(select(User.id, User.email, User.plan, User.stripe_customer_id)).all()
        by_user = defaultdict(list)
        rows = db.execute(
            select(
                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                ScraperConfig.record_type, ScraperConfig.created_at,
                ScraperConfig.active, ScraperConfig.paused_reason,
                ScraperConfig.user_id,
            )
        ).all()
        for r in rows:
            by_user[str(r.user_id)].append(ConfigRow(r.id, r.state, r.county, r.record_type, r.created_at, r.active, r.paused_reason))
        affected = 0
        for uid, email, plan, cust in users:
            pause, _ = plan_reconciliation(by_user.get(str(uid), []), plan or "starter")
            if pause:
                affected += 1
                paid = "PAID" if cust else "free/trial"
                print(f"{email}\tplan={plan}\t{paid}\twould_pause={len(pause)} configs")
        print(f"\nTOTAL affected users: {affected}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run against prod (read-only)**

Run: `railway run python scripts/audit_entitlement_violations.py` (or the project's prod-DSN invocation)
Expected: a list. **Decision gate:** if any `PAID` user appears, add a time-bounded grandfather override before flipping (note it back to the user; do not flip yet). If only free/trial users appear, proceed.

- [ ] **Step 3: Flip the flag (ops, both services)**

Set `ENTITLEMENT_ENFORCEMENT=true` on the **API** and **worker** Railway services. No code change. Document in `docs/BUILD_JOURNAL.md`.

- [ ] **Step 4: Commit the script + journal**

```bash
git add scripts/audit_entitlement_violations.py docs/BUILD_JOURNAL.md
git commit -m "chore(entitlements): audit script + enforcement flip notes (Phase 6)"
```

**PHASE 6 GATE:** audit reviewed with user; flag flipped only after the paid-user decision. User approval before Phase 7.

---

# PHASE 7 — Live per-tier verification (proof)

### Task 13: Verify every gate live

**Files:** none (verification only; capture evidence in `docs/BUILD_JOURNAL.md`).

- [ ] **Step 1: Starter blocked from premium type.** Create a Starter user; attempt to create/run a `pre_foreclosure` config → expect 402 at create AND, for a pre-existing one, the worker fails the job with "Plan limit reached". Capture the response + job log.

- [ ] **Step 2: Pro county cap.** Pro user with 3 counties; create a 4th → 402. Run a job for the 4th (if one slipped in) → worker blocks. Capture.

- [ ] **Step 3: Pro premium type.** Pro user attempts `divorce` → 402. Capture.

- [ ] **Step 4: Trial expiry → downgrade → reconciliation.** Set a user `trial_ends_at` in the past with Business configs (3+ counties, a premium type); run the trial-expiry beat task; confirm over-limit configs flip to `active=false, paused_reason='entitlement'`; next scheduled dispatch skips them. Capture DB rows + scheduler log.

- [ ] **Step 5: API key after downgrade.** Downgrade a Business user to Pro; call an API endpoint with their existing `bl_` key → 403. Capture.

- [ ] **Step 6: Generic webhook after downgrade.** Downgraded Business user's completed job → webhook NOT sent, job log shows "Webhook delivery skipped — requires Business plan". Capture.

- [ ] **Step 7: Re-upgrade revives.** Upgrade the Step 4 user back to Business; reconciliation revives the entitlement-paused configs (`active=true, paused_reason=null`). Capture.

- [ ] **Step 8: Write the BUILD_JOURNAL entry** summarizing built/verified per the file's format, and update memory.

**PHASE 7 GATE:** all 7 gates demonstrated with evidence; Codex final `codex review` of the full branch diff. Done.

---

## Self-review notes (author)

- **Spec coverage:** §4.2 A→Task 1; §4.2 B→Tasks 2-8; §4.2 C→Tasks 10-11; §4.2 D→Task 9; §4.3 migration→Task 10; §5 rollout→phase grouping; §6 testing→per-task tests + Task 13; §7 risks→audit gate (Task 12) + `paused_reason` marker (Task 10) + `_candidate_rows` user-pause exclusion (Task 2).
- **Ordering caveat:** Task 3/4/5/6 select `ScraperConfig.paused_reason`, added in Task 10 (Phase 5). If executing strictly in phase order, run Task 10's migration + model column FIRST (it's additive and safe), then Phase 3. Flagged inline in Task 3 Step 3.
- **Type consistency:** `ConfigRow(*r)` relies on SELECT column order matching the dataclass field order (id, state, county, record_type, created_at, active, paused_reason) — every call site uses that exact order.
