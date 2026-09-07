"""The 402 body on the wire, end to end through the real route.

The frontend renders `detail.title` and `detail.message` directly. If this shape
regresses to a bare string, the notice silently loses its heading; if it
regresses to something else, the customer reads `[object Object]`. Neither
failure shows up in a unit test of the copy builders, and neither shows up in
CI's schema check either: schema/openapi.json declares no 402 responses at all,
so nothing else in this repo guards this contract.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.db.models import ScraperConfig, User


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_body(county: str, record_type: str = "probate") -> dict:
    return {
        "name": f"Wire contract {county}",
        "county": county,
        "state": "WA",
        "record_type": record_type,
    }


async def _existing_config(db: AsyncSession, user: User, county: str) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=f"Existing {county}",
        county=county,
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    return config


@pytest.fixture
def enforcement_on(monkeypatch):
    """The gate ships dark (ENTITLEMENT_ENFORCEMENT=False, audit-only). Nothing
    402s until it is on, so the contract can only be observed with it flipped."""
    monkeypatch.setattr(settings, "ENTITLEMENT_ENFORCEMENT", True)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_county_returns_a_structured_402_body(
    client, db, starter_user, starter_token, enforcement_on
):
    """The exact case from the bug report: Starter, one saved county, add another."""
    await _existing_config(db, starter_user, "king")

    r = await client.post(
        "/scrapers", json=_create_body("pierce"), headers=_auth(starter_token)
    )

    assert r.status_code == 402
    body = r.json()
    assert body == {
        "detail": {
            "code": "county_limit",
            "title": "County limit reached",
            "message": (
                "Your Starter plan includes 1 county. "
                "This would put your account at 2 counties. "
                "Upgrade your plan to continue."
            ),
        }
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_402_message_never_carries_an_em_dash_or_a_quoted_slug(
    client, db, starter_user, starter_token, enforcement_on
):
    await _existing_config(db, starter_user, "king")
    r = await client.post(
        "/scrapers", json=_create_body("pierce"), headers=_auth(starter_token)
    )
    detail = r.json()["detail"]
    for text in (detail["title"], detail["message"]):
        assert "—" not in text  # em dash, banned in user-facing copy
        assert "–" not in text  # en dash
        assert "'starter'" not in text
        assert "distinct" not in text.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_disallowed_record_type_returns_its_own_title(
    client, db, starter_user, starter_token, enforcement_on
):
    """Starter covers probate only. This must NOT be titled a county limit.

    king + pre_foreclosure, not king + divorce: the route runs
    `_validate_connector_supports` BEFORE the entitlement gate, so a record type
    with no live connector in that county 422s as unavailable and never reaches
    the plan check at all. The pair here has to be one the county actually
    offers, or the test proves nothing about entitlements.
    """
    r = await client.post(
        "/scrapers",
        json=_create_body("king", record_type="pre_foreclosure"),
        headers=_auth(starter_token),
    )
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["code"] == "record_type"
    assert detail["title"] == "Record type not in your plan"
    assert detail["message"] == (
        "Pre-Foreclosure is not included in your Starter plan. "
        "Upgrade your plan to continue."
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_breaking_both_limits_at_once_returns_one_combined_notice(
    client, db, starter_user, starter_token, enforcement_on
):
    await _existing_config(db, starter_user, "king")
    r = await client.post(
        "/scrapers",
        json=_create_body("pierce", record_type="divorce"),
        headers=_auth(starter_token),
    )
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["code"] == "plan_limit"
    assert detail["title"] == "Plan limit reached"
    assert "Divorce is not included in your Starter plan." in detail["message"]
    assert "This would put your account at 2 counties." in detail["message"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_first_county_within_the_plan_is_not_refused(
    client, db, starter_user, starter_token, enforcement_on
):
    """Guard against the notice firing on a legitimate create."""
    r = await client.post(
        "/scrapers", json=_create_body("king"), headers=_auth(starter_token)
    )
    assert r.status_code != 402


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nothing_is_refused_while_enforcement_is_still_dark(
    client, db, starter_user, starter_token
):
    """No `enforcement_on` fixture here. The flag defaults off in production, and
    the copy change must not have turned an audit log into a live gate."""
    assert settings.ENTITLEMENT_ENFORCEMENT is False
    await _existing_config(db, starter_user, "king")
    r = await client.post(
        "/scrapers", json=_create_body("pierce"), headers=_auth(starter_token)
    )
    assert r.status_code != 402
