"""scraper_mode 'template' resolves the recorder-platform template from base_url;
'manual' imports the configured class. The old name 'ai' is retired (AI-mode removal,
2a read both, 2b migrated the rows, 2c dropped the alias and the DB refuses it).

Real DB rows; the registry is the production resolver the worker, the canary and
GET /scrapers all use.
"""
import uuid

import pytest
from sqlalchemy import delete

from src.db.models import CountyConnector
from src.scrapers.base_scraper import BridgeScraper
from src.scrapers.registry import connector_scraper_class, get_scraper_class
from src.scrapers.templates.eagleweb import EagleWebScraper


@pytest.fixture
async def connector(db):
    """Create one active connector; every row made here is deleted afterwards."""
    made: list[str] = []

    async def make(mode: str, *, template_url: bool = True) -> CountyConnector:
        county = f"tmpl{uuid.uuid4().hex[:8]}"
        row = CountyConnector(
            id=str(uuid.uuid4()), county=county, state="WA", record_types=["probate"],
            scraper_class="" if template_url else "src.scrapers.base_scraper.BridgeScraper",
            scraper_mode=mode,
            base_url=f"https://eagleweb.{county}.example.gov/recorder/" if template_url
            else f"https://{county}.example.gov",
            health_status="healthy", active=True,
        )
        db.add(row)
        await db.commit()
        made.append(row.id)
        return row

    yield make
    await db.execute(delete(CountyConnector).where(CountyConnector.id.in_(made)))
    await db.commit()


async def test_a_template_connector_resolves_its_template(connector):
    row = await connector("template")

    factory, record_type = get_scraper_class(row.county, "WA", "probate")
    assert (factory.func, factory.keywords["base_url"], record_type) == (
        EagleWebScraper, row.base_url, "probate",
    )
    assert connector_scraper_class(row) is EagleWebScraper


async def test_a_manual_connector_is_untouched(connector):
    row = await connector("manual", template_url=False)

    factory, _ = get_scraper_class(row.county, "WA", "probate")
    assert factory is BridgeScraper
    assert connector_scraper_class(row) is BridgeScraper


def test_only_template_means_template_mode():
    """REGRESSION (2c): 'ai' was still read as template until the alias was retired."""
    from src.scrapers.registry import is_template_mode

    assert is_template_mode("template")
    assert not any(is_template_mode(m) for m in ("ai", "manual", "", None, "Template", "AI"))
