"""The batch date-range choice → per-child schedule → resolved-window contract.

Covers the Codex-reviewed design: "recommended"/default keeps the per-record-type
window (tax_delinquent ~18mo, others 90d); "custom"/"since_last_run" override the
window uniformly across every child; malformed custom ranges 422 at the boundary.

Pure functions — no DB — but the module import still requires the suite's test-DB
env guard (conftest), so this lives in the normal test tree.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException

from src.api.routes.batches import _parse_user_date, _resolve_batch_child_schedule
from src.workers.tasks_helpers.dates import (
    _TAX_DELINQUENT_DEFAULT_DAYS,
    _resolve_date_range,
)


def _window_days(date_from: str, date_to: str) -> int:
    d0 = datetime.strptime(date_from, "%m/%d/%Y").date()
    d1 = datetime.strptime(date_to, "%m/%d/%Y").date()
    return (d1 - d0).days


# ── mapping: choice → per-child schedule ──────────────────────────────────────


@pytest.mark.parametrize("mode", ["recommended", "rolling_90", "", "  ", "anything", None])
def test_recommended_and_default_produce_empty_child_schedule(mode):
    # Anything that isn't an explicit override leaves the child schedule empty so
    # _resolve_date_range keeps the per-record-type default. This is what stops a
    # mixed batch from silently shrinking tax-delinquent history to 90d.
    assert _resolve_batch_child_schedule(mode, None, None) == {}


def test_since_last_run_maps_verbatim():
    assert _resolve_batch_child_schedule("since_last_run", None, None) == {
        "date_range_mode": "since_last_run"
    }


def test_custom_iso_maps_to_iso_window():
    assert _resolve_batch_child_schedule("custom", "2026-01-01", "2026-03-31") == {
        "date_range_mode": "custom",
        "date_from": "2026-01-01",
        "date_to": "2026-03-31",
    }


def test_custom_accepts_mm_dd_yyyy_and_normalizes_to_iso():
    assert _resolve_batch_child_schedule("custom", "01/01/2026", "03/31/2026") == {
        "date_range_mode": "custom",
        "date_from": "2026-01-01",
        "date_to": "2026-03-31",
    }


@pytest.mark.parametrize(
    "raw_from,raw_to",
    [(None, "2026-03-31"), ("2026-01-01", None), ("", ""), ("2026-01-01", "")],
)
def test_custom_missing_dates_422(raw_from, raw_to):
    with pytest.raises(HTTPException) as exc:
        _resolve_batch_child_schedule("custom", raw_from, raw_to)
    assert exc.value.status_code == 422


def test_custom_inverted_range_422():
    with pytest.raises(HTTPException) as exc:
        _resolve_batch_child_schedule("custom", "2026-03-31", "2026-01-01")
    assert exc.value.status_code == 422


def test_parse_user_date_rejects_garbage():
    with pytest.raises(HTTPException) as exc:
        _parse_user_date("not-a-date")
    assert exc.value.status_code == 422


# ── end-to-end: child schedule → resolved scrape window ───────────────────────


def test_recommended_child_preserves_per_type_window():
    child = _resolve_batch_child_schedule("recommended", None, None)  # {}
    df, dt = _resolve_date_range(child, record_type="tax_delinquent")
    assert _window_days(df, dt) == _TAX_DELINQUENT_DEFAULT_DAYS
    df2, dt2 = _resolve_date_range(child, record_type="probate")
    assert _window_days(df2, dt2) == 90


def test_custom_child_overrides_tax_default():
    child = _resolve_batch_child_schedule("custom", "2026-01-01", "2026-02-01")
    # An explicit custom window is honored verbatim — no 18mo tax upgrade.
    df, dt = _resolve_date_range(child, record_type="tax_delinquent")
    assert (df, dt) == ("01/01/2026", "02/01/2026")
