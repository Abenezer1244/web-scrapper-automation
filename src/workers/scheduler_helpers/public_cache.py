"""Body logic for the refresh_public_sample_cache beat task."""

from src.utils.logger import setup_logger

_logger = setup_logger("worker.scheduler")


def _refresh_public_sample_cache_impl() -> None:
    """Recompute the sanitized landing-page samples + stats into
    public_sample_cache (RLS cutover Phase 2b).

    The public /scrapers/sample endpoint reads ONLY this precomputed row, so an
    unauthenticated request never live-queries the tenant tables
    (results/jobs/scraper_configs) and the API role needs no cross-tenant read
    policy for it. ALL PII redaction happens HERE, so the cached payload is safe
    to serve publicly. Runs via system_sync_session (cross-tenant, no RLS user
    context) — under the cutover the bridgeleads_system FOR ALL policy applies.
    """
    import json

    from sqlalchemy import func as sa_func
    from sqlalchemy import select, text

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import system_sync_session

    def _generalize_address(addr: str | None) -> str | None:
        # Drop the leading street segment; keep city/state/ZIP. 1 part (no
        # comma) → can't isolate the street safely, so redact. (mirrors N2)
        if not addr:
            return None
        parts = [p.strip() for p in addr.split(",") if p.strip()]
        return ", ".join(parts[1:]) if len(parts) >= 2 else None

    def _fmt_count(n: int) -> str:
        if n < 1000:
            return f"{n}+"
        if n < 10_000:
            return f"{round(n / 1000, 1)}K+"
        return f"{n // 1000:,}K+"

    with system_sync_session() as db:
        rows = db.execute(
            select(Result, ScraperConfig.county)
            .join(Job, Result.job_id == Job.id)
            .join(ScraperConfig, Job.scraper_config_id == ScraperConfig.id)
            .where(
                Job.status == "done",
                Result.property_address.isnot(None),
                Result.property_address != "",
                Result.mailing_address.isnot(None),
                Result.mailing_address != "",
            )
            .order_by(Job.created_at.desc())
            .limit(5)
        ).all()

        samples = []
        for r, county_slug in rows:
            # Partially anonymize: first name + last initial.
            name = r.party_name or ""
            parts = name.split()
            if len(parts) >= 2:
                anon_name = f"{parts[0]} {parts[1][0]}."
            else:
                anon_name = f"{parts[0][0]}." if parts else "—"
            samples.append({
                # date_recorded is String(32) in the model, not a date — store
                # it verbatim (matches the old inline /sample behavior exactly).
                "date_recorded": r.date_recorded,
                "party_name": anon_name,
                "county": (county_slug or "").title(),
                "property_address": _generalize_address(r.property_address),
                "mailing_address": _generalize_address(r.mailing_address),
                "has_parcel": bool(r.parcel_id),
            })

        delivered_count = db.execute(
            select(sa_func.count(Result.id)).where(
                Result.property_address.isnot(None),
                Result.property_address != "",
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
