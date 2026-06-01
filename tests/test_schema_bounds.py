"""Input-bound tests for request schemas (security review Medium: unbounded fields).

Caps prevent oversized-body DoS (bcrypt CPU, huge lists/strings parsed before
validators). Valid inputs must still pass; abusive ones rejected (or, for the
silently-dropped referral code, neutralized).
"""
import pytest
from pydantic import ValidationError

from src.api.schemas import (
    ConnectorCreate,
    DeliverConfig,
    JobCreate,
    PasswordChange,
    ScheduleConfig,
    ScraperConfigCreate,
    UserLogin,
    UserRegister,
)


def _valid_config(**over):
    base = {"name": "Pierce Probate", "county": "pierce", "state": "wa", "record_type": "probate"}
    base.update(over)
    return ScraperConfigCreate(**base)


def test_valid_inputs_accepted():
    UserLogin(email="a@b.co", password="secret1234")
    cfg = _valid_config(state=" wa ")  # padded state preserved + normalized
    assert cfg.state == "WA"
    JobCreate(scraper_config_id="550e8400-e29b-41d4-a716-446655440000")
    ConnectorCreate(county="king", state="WA", record_types=["probate"], base_url="https://x.gov")
    DeliverConfig(emails=["a@b.co"], formats=["csv", "excel"])
    ScheduleConfig(frequency="daily", run_at_hour=6, run_at_minute=30)


@pytest.mark.parametrize(
    "fn",
    [
        lambda: UserLogin(email="a@b.co", password="x" * 100_000),
        lambda: PasswordChange(current_password="x" * 100_000, new_password="secret1234"),
        lambda: _valid_config(state="Washington"),
        lambda: _valid_config(name="z" * 200),
        lambda: _valid_config(county="c" * 100),
        lambda: JobCreate(scraper_config_id="z" * 5000),
        lambda: ConnectorCreate(county="k", state="WA", record_types=["x"] * 50, base_url="https://x.gov"),
        lambda: ConnectorCreate(county="k", state="WA", record_types=["y" * 100], base_url="https://x.gov"),
        lambda: DeliverConfig(emails=["a@b.co"] * 50),
        lambda: DeliverConfig(formats=["z" * 100]),
        lambda: ScheduleConfig(run_at_hour=99),
        lambda: ScheduleConfig(run_at_minute=99),
    ],
)
def test_abusive_inputs_rejected(fn):
    with pytest.raises(ValidationError):
        fn()


def test_oversized_referral_code_silently_dropped():
    # By design ref codes are dropped (not rejected) so existing codes aren't leaked.
    u = UserRegister(email="a@b.co", password="secret1234", ref="Z" * 100_000)
    assert u.ref is None
