"""Clark auction leads fail clearly instead of reporting a zero the source cannot prove
(owner decision 2026-11-14, tasks/clark-wa-seven.md)."""
import pytest

from src.scrapers import trustee_sale
from src.scrapers.reliability import (
    ScraperExecutionError,
    SourceUnavailableError,
    TransientScrapeError,
    is_transient_scrape_error,
)


async def test_clark_trustee_sale_fails_with_reason_before_reading_cache():
    with pytest.raises(ScraperExecutionError, match="temporarily unavailable") as exc:
        await trustee_sale.ClarkWATrusteeSaleScraper().scrape("08/16/2026", "11/14/2026")
    # Permanent: the worker must not burn retries on a source that cannot answer.
    assert not isinstance(exc.value, TransientScrapeError)
    assert not is_transient_scrape_error(exc.value)
    # The worker shows exactly this text to the customer (tasks.py SourceUnavailableError).
    assert isinstance(exc.value, SourceUnavailableError)
    assert exc.value.public_message.startswith("Auction leads for Clark County")
    assert "—" not in exc.value.public_message  # no em dash in customer copy


def test_only_clark_is_on_hold():
    held = {c.COUNTY for c in trustee_sale._TrusteeSaleScraper.__subclasses__()
            if c.SOURCE_UNAVAILABLE}
    assert held == {"clark"}


# ─── through the real worker (real test DB + Redis; the real connector class) ────

@pytest.fixture
def clark_hold_job(db):  # db: the shared fixture deletes the user, config and job
    import uuid
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from src.api.auth import hash_password
    from src.db.models import CountyConnector, Job, ScraperConfig, User
    from src.db.session import SyncSessionLocal

    county = f"clarkhold-{uuid.uuid4().hex[:8]}"
    start = datetime.now(UTC) - timedelta(days=10)
    with SyncSessionLocal() as s:
        user = User(
            id=str(uuid.uuid4()), email=f"clarkhold_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
            password_hash=hash_password("TestPass123!"), plan="business",
            records_limit=5000, records_used=0, subscription_status="active",
            quota_anchor_at=start, quota_period_start=start,
            quota_period_end=start + timedelta(days=30), records_period_start=start,
        )
        connector = CountyConnector(
            id=str(uuid.uuid4()), county=county, state="WA", record_types=["trustee_sale"],
            scraper_class="src.scrapers.trustee_sale.ClarkWATrusteeSaleScraper",
            scraper_mode="manual", render_mode="static",
            base_url="https://classifieds.columbian.com/subcategories/view/55",
            health_status="healthy", active=True,
        )
        s.add_all([user, connector])
        s.flush()
        config = ScraperConfig(
            id=str(uuid.uuid4()), user_id=user.id, name="clark hold", county=county,
            state="WA", record_type="trustee_sale", fields=["party_name"], enrichment=[],
            schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
            active=True,
        )
        s.add(config)
        s.flush()
        job = Job(id=str(uuid.uuid4()), user_id=user.id, scraper_config_id=config.id,
                  status="pending", trigger="manual")
        s.add(job)
        s.commit()
        ids = (job.id, user.id, connector.id)
    yield ids
    with SyncSessionLocal() as s:
        s.execute(text("DELETE FROM county_connectors WHERE id = :c"), {"c": ids[2]})
        s.commit()


def test_worker_shows_the_hold_message_and_never_retries_or_bills(clark_hold_job, redis_client):
    from sqlalchemy import text

    from src.db.session import SyncSessionLocal
    from src.workers import tasks

    job_id, user_id, _ = clark_hold_job
    tasks.run_scrape_job.apply(args=[job_id])
    with SyncSessionLocal() as s:
        job = s.execute(text(
            "SELECT status, error_message, retry_count, billing_applied_at, billed_count, "
            "reserved_count FROM jobs WHERE id = :j"), {"j": job_id}).one()
        used = s.execute(text("SELECT records_used FROM users WHERE id = :u"),
                         {"u": user_id}).scalar_one()
    assert job.status == "failed"
    assert job.error_message == trustee_sale.ClarkWATrusteeSaleScraper.SOURCE_UNAVAILABLE
    assert job.retry_count == 0
    assert job.billing_applied_at is None and not job.billed_count
    assert not job.reserved_count
    assert used == 0
