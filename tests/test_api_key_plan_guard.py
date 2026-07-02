# tests/test_api_key_plan_guard.py
from src.config.constants import BUSINESS_FEATURES_PLANS


def test_api_access_is_business_plus():
    assert "business" in BUSINESS_FEATURES_PLANS
    assert "agency" in BUSINESS_FEATURES_PLANS
    assert "pro" not in BUSINESS_FEATURES_PLANS
    assert "starter" not in BUSINESS_FEATURES_PLANS
