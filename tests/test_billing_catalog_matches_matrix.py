"""The customer-facing plan catalog must not promise access the matrix denies."""
from src.api.routes.billing import _PLANS
from src.config.constants import COUNTY_LIMIT_BY_PLAN, RECORD_TYPES_BY_PLAN


def _plan(pid: str) -> dict:
    return next(p for p in _PLANS if p["id"] == pid)


def test_pro_does_not_claim_all_record_types():
    feats = " ".join(_plan("pro")["features"]).lower()
    # Pro is the 4 core lists (incl. Auction Leads), NOT all types.
    assert "all record types" not in feats
    assert len(RECORD_TYPES_BY_PLAN["pro"]) == 4


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
