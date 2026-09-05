"""GET /scrapers batch-child visibility + BatchSummary.counties.

Regression cover for the "one user scrape = one visible row" fix. A batch fans
out into one child scraper_config per county x record_type; those children are
implementation rows, not scrapes the user started, so the Scrapers list must be
able to drop them — WITHOUT changing the default, which other callers rely on.
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ScraperBatch, ScraperConfig, User


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _batch_with_children(db: AsyncSession, user: User) -> tuple[str, list[str]]:
    """A real batch parent + two children (king/probate, king/pre_foreclosure),
    wired through the same batch_id FK the API groups on."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name="regression batch",
        state="WA",
        fields={},
        enrichment={},
        schedule={},
        deliver={},
        delivery_mode="everything",
        status="active",
    )
    db.add(batch)
    await db.flush()

    child_ids = []
    for county, rt in (("king", "probate"), ("pierce", "pre_foreclosure")):
        c = ScraperConfig(
            id=str(uuid.uuid4()),
            user_id=user.id,
            name=f"regression batch - {county} {rt}",
            county=county,
            state="WA",
            record_type=rt,
            fields={},
            enrichment={},
            schedule={},
            deliver={},
            batch_id=batch.id,
            active=True,
        )
        db.add(c)
        child_ids.append(c.id)
    await db.commit()
    return batch.id, child_ids


@pytest.mark.asyncio
async def test_default_still_returns_batch_children(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    """The DEFAULT must not change. Two callers depend on seeing every config:
    the grandfathered-probate TOD notice counts EVERY probate config (a batch
    child can be one), and /scrapers/{id}/records resolves a child out of this
    list. Flipping the default would silently break both."""
    _batch_id, child_ids = await _batch_with_children(db, starter_user)

    r = await client.get("/scrapers", headers=_auth(starter_token))
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()}
    for cid in child_ids:
        assert cid in ids, "batch children must still be visible by default"
    assert scraper_config.id in ids


@pytest.mark.asyncio
async def test_exclude_batch_children_drops_only_children(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
    scraper_config: ScraperConfig,
):
    _batch_id, child_ids = await _batch_with_children(db, starter_user)

    r = await client.get(
        "/scrapers?exclude_batch_children=true", headers=_auth(starter_token)
    )
    assert r.status_code == 200
    rows = r.json()
    ids = {c["id"] for c in rows}
    for cid in child_ids:
        assert cid not in ids, "a batch child is not a top-level scrape"
    # the standalone config is untouched — only children are dropped
    assert scraper_config.id in ids
    assert all(c["batch_id"] is None for c in rows)


@pytest.mark.asyncio
async def test_batch_id_is_exposed(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
):
    """Without batch_id on the response the frontend cannot tell a child from a
    standalone scrape at all — which is how they came to be rendered as peers."""
    batch_id, child_ids = await _batch_with_children(db, starter_user)

    r = await client.get("/scrapers", headers=_auth(starter_token))
    by_id = {c["id"]: c for c in r.json()}
    for cid in child_ids:
        assert by_id[cid]["batch_id"] == batch_id


@pytest.mark.asyncio
async def test_batch_summary_reports_counties_and_types(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str,
):
    """counties mirrors record_types so a collapsed batch row can name what it
    covers without one detail request per batch."""
    batch_id, _ = await _batch_with_children(db, starter_user)

    r = await client.get("/batches", headers=_auth(starter_token))
    assert r.status_code == 200
    row = next(b for b in r.json() if b["id"] == batch_id)
    assert sorted(row["counties"]) == ["king", "pierce"]
    assert sorted(row["record_types"]) == ["pre_foreclosure", "probate"]
    assert row["child_count"] == 2


@pytest.mark.asyncio
async def test_children_of_another_tenant_are_never_returned(
    client: AsyncClient, db: AsyncSession, starter_user: User,
    business_user: User, business_token: str,
):
    """Grouping keys off (batch_id, user_id) — a second tenant must not see the
    first tenant's children under either flag value."""
    _batch_id, child_ids = await _batch_with_children(db, starter_user)

    for qs in ("", "?exclude_batch_children=true"):
        r = await client.get(f"/scrapers{qs}", headers=_auth(business_token))
        assert r.status_code == 200
        ids = {c["id"] for c in r.json()}
        assert not (ids & set(child_ids))
