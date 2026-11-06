"""Body logic for the refresh_public_sample_cache beat task."""

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")

# Illustrative records for the public /scrapers/sample endpoint. These describe
# no real person. The endpoint is unauthenticated, and the previous build served
# real owners' first names, last initials, ZIP+4 and recording dates, which could
# be matched back to the county recorder index (compliance audit 2026-11, P1-6).
# Every record carries example=True so a consumer can never mistake it for data.
_EXAMPLE_RECORDS: tuple[dict, ...] = (
    {"record_type": "probate", "date_recorded": "Example", "party_name": "Example owner",
     "county": "King", "property_address": None, "mailing_address": "Seattle, WA, 98101",
     "has_parcel": True, "example": True},
    {"record_type": "pre_foreclosure", "date_recorded": "Example", "party_name": "Example owner",
     "county": "Pierce", "property_address": None, "mailing_address": "Tacoma, WA, 98402",
     "has_parcel": True, "example": True},
    {"record_type": "tax_delinquent", "date_recorded": "Example", "party_name": "Example owner",
     "county": "Snohomish", "property_address": None, "mailing_address": "Everett, WA, 98201",
     "has_parcel": True, "example": True},
    {"record_type": "trustee_sale", "date_recorded": "Example", "party_name": "Example owner",
     "county": "Clark", "property_address": None, "mailing_address": "Vancouver, WA, 98660",
     "has_parcel": True, "example": True},
    {"record_type": "code_violation", "date_recorded": "Example", "party_name": "Example owner",
     "county": "King", "property_address": None, "mailing_address": "Bellevue, WA, 98004",
     "has_parcel": True, "example": True},
)


def example_sample_records() -> list[dict]:
    """Return fresh copies of the illustrative sample records (no real data)."""
    return [dict(r) for r in _EXAMPLE_RECORDS]


def _refresh_public_sample_cache_impl() -> None:
    """Recompute the sanitized landing-page samples + stats into
    public_sample_cache (RLS cutover Phase 2b).

    The public /scrapers/sample endpoint reads ONLY this precomputed row, so an
    unauthenticated request never live-queries the tenant tables
    (results/jobs/scraper_configs) and the API role needs no cross-tenant read
    policy for it. Only aggregate counts come from the database; the records are
    the illustrative _EXAMPLE_RECORDS, so no person's data is ever published here.
    Runs via system_sync_session (cross-tenant, no RLS user context) — under the
    cutover the bridgeleads_system FOR ALL policy applies.
    """
    import json

    from sqlalchemy import func as sa_func
    from sqlalchemy import select, text

    from src.api.lead_actionability import not_privacy_suppressed_condition
    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session

    def _fmt_count(n: int) -> str:
        if n < 1000:
            return f"{n}+"
        if n < 10_000:
            return f"{round(n / 1000, 1)}K+"
        return f"{n // 1000:,}K+"

    samples = example_sample_records()

    with system_sync_session() as db:
        # Public, cross-tenant marketing numbers: a privacy-withheld row is not one.
        delivered_count = db.execute(
            select(sa_func.count(Result.id)).where(
                Result.property_address.isnot(None),
                Result.property_address != "",
                not_privacy_suppressed_condition(),
            )
        ).scalar() or 0

        counties_active = db.execute(
            select(sa_func.count(sa_func.distinct(ScraperConfig.county)))
            .select_from(Result)
            .join(Job, Result.job_id == Job.id)
            .join(ScraperConfig, Job.scraper_config_id == ScraperConfig.id)
            .where(
                Result.property_address.isnot(None),
                Result.property_address != "",
                not_privacy_suppressed_condition(),
            )
        ).scalar() or 0

        payload = {
            "records": samples,
            "total_scraped": _fmt_count(delivered_count),
            "counties_active": counties_active,
            "enrichment_rate": "95%+",
            "freshness": "Updated daily",
        }

        # Singleton upsert (id is fixed at 1 by the table default + CHECK).
        db.execute(
            text("""
                INSERT INTO public.public_sample_cache (id, payload, refreshed_at)
                VALUES (1, CAST(:payload AS jsonb), now())
                ON CONFLICT (id) DO UPDATE
                    SET payload = EXCLUDED.payload, refreshed_at = now()
            """),
            {"payload": json.dumps(payload)},
        )
        db.commit()
        _logger.info("Refreshed public_sample_cache: %d samples", len(samples))
