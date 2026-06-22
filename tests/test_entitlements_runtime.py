# tests/test_entitlements_runtime.py
from datetime import UTC, datetime, timedelta

from src.api.entitlements import (
    PAUSED_REASON_ENTITLEMENT,
    ConfigRow,
    allowed_county_set,
    config_run_violation,
    plan_reconciliation,
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
