"""AI mode removal, Phase 1: there is no monthly "AI" run cap any more.

Owner decision 2026-09-30: remove AI mode from the product. The template
connectors (still stored as scraper_mode 'ai' until Phase 2) are ordinary
scrapers; running one no longer spends a per-plan monthly allowance. Runs stay
bounded by the record quota and the entitlement rules, which are unchanged.
"""
import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from tests.test_config_eligibility import _config, _county, _eligibility, _job, _post, _user


async def test_a_template_scraper_keeps_running_past_the_old_monthly_cap(
    db, connectors, client: AsyncClient,
):
    """REGRESSION: a Starter account with 6 runs this month on a template county.
    The old cap refused the 6th ("Monthly AI scrape limit reached (5/5)")."""
    county = _county()
    await connectors(county, ["probate"], "ai")
    user = await _user(db, plan="starter", records_limit=50)
    config = await _config(db, user, county)
    for _ in range(6):
        await _job(db, user, config)

    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.can_run, e.code, e.message) == (True, None, None)
    r = await _post(client, user, config)
    assert r.status_code == 201, r.text


async def test_the_record_quota_still_refuses_a_template_scraper(db, connectors, client: AsyncClient):
    """The cap's removal lifts nothing else: an account over its record limit is
    still refused on a template county, with the account rule's code."""
    county = _county()
    await connectors(county, ["probate"], "ai")
    user = await _user(db, plan="starter", records_limit=50, records_used=50)
    config = await _config(db, user, county)

    e = (await _eligibility(db, user, [config]))[config.id]
    assert (e.can_run, e.code) == (False, "over_limit")
    r = await _post(client, user, config)
    assert r.status_code == 402, r.text


def test_ai_limit_is_no_longer_a_refusal_code():
    """The API contract: `ai_limit` is gone from every place a client reads codes."""
    from src.api.errors import RUN_REFUSAL_CODES
    from src.api.schemas import ConfigRunEligibilityResponse, RunRefusalResponse

    assert "ai_limit" not in RUN_REFUSAL_CODES
    with pytest.raises(ValidationError):
        ConfigRunEligibilityResponse(can_run=False, code="ai_limit", message="x")
    with pytest.raises(ValidationError):
        RunRefusalResponse(detail="x", code="ai_limit", resumes_at=None)


def test_the_plan_table_has_no_ai_run_limits():
    from src.config import settings

    assert not hasattr(settings, "AI_JOB_LIMITS")
