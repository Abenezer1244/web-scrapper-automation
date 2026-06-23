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


## GLOBAL CONSTRAINTS (bind this task)

- Canonical matrix = `docs/pricing-strategy-2026-06.md` §4 = `src/config/constants.py` (`COUNTY_LIMIT_BY_PLAN:149`, `RECORD_TYPES_BY_PLAN:170`). Do NOT change those values.
- Counties = count-based, user-choice. Record types = capability menu (Starter `probate`; Pro `probate`/`pre_foreclosure`/`tax_delinquent`; Business+Agency all 6).
- All new checks gated by `settings.ENTITLEMENT_ENFORCEMENT` (default False): OFF = log audit + proceed; ON = block (402 API / fail job / skip dispatch).
- Fail CLOSED on unknown plan = `starter` (existing `entitlements.py` convention).
- No mocks/dummies; tests use real DB + real settings (`.claude/rules/testing.md`). `_norm_county(state, county)` is the single normalization helper — reuse it.
- Multi-tenant: every query filters by `user_id` (RLS belt + filter suspenders).
- Each phase ≤5 files; complete + verify a phase, then get explicit user approval before the next (CLAUDE.md). Codex reviews every phase diff; any Critical/High = NO-GO until fixed.
- Errors to clients carry no stack trace/raw DB error.

