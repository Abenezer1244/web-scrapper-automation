"""Celery task: full scrape job lifecycle.

State machine:
    PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE
                                                        → FAILED
"""

import asyncio
import json
from datetime import UTC, datetime

import redis as sync_redis

from src.config import settings
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.task")


def _now() -> datetime:
    return datetime.now(UTC)


def _redis() -> sync_redis.Redis:
    return sync_redis.from_url(settings.REDIS_URL, decode_responses=True)


def _publish_log(r: sync_redis.Redis, job_id: str, level: str, message: str) -> None:
    """Publish a log line to Redis Pub/Sub and persist it to the DB."""
    import uuid

    from src.db.models import JobLog
    from src.db.session import SyncSessionLocal

    payload = {
        "id": str(uuid.uuid4()),
        "level": level,
        "message": message,
        "created_at": _now().isoformat(),
        "type": "log",
    }
    r.publish(f"job_logs:{job_id}", json.dumps(payload))

    # Persist to DB for SSE replay
    with SyncSessionLocal() as db:
        db.add(JobLog(
            id=payload["id"],
            job_id=job_id,
            level=level,
            message=message,
        ))
        db.commit()


def _set_status(db, job, status: str, **kwargs) -> None:
    """Update job status and any extra fields, then commit."""
    job.status = status
    for k, v in kwargs.items():
        setattr(job, k, v)
    db.commit()
    db.refresh(job)


@app.task(
    name="src.workers.tasks.run_scrape_job",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=3600,  # 60 min (scrape + enrichment in one job)
    time_limit=3900,       # 65 min
)
def run_scrape_job(self, job_id: str) -> None:
    """Execute a full scrape job lifecycle for the given job_id."""
    from sqlalchemy import select

    from src.db.models import Job, Result, ScraperConfig, User
    from src.db.session import SyncSessionLocal
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class
    from src.utils.data_exporter import DataExporter
    from src.workers.delivery import deliver_job_results

    r = _redis()

    with SyncSessionLocal() as db:
        # ── Load job ─────────────────────────────────────────────────────────
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if job is None:
            _logger.error("Job %s not found — aborting", job_id)
            return

        if job.status == "cancelled":
            _logger.info("Job %s was cancelled before worker picked it up", job_id)
            return

        config = db.execute(
            select(ScraperConfig).where(ScraperConfig.id == job.scraper_config_id)
        ).scalar_one()

        user = db.execute(select(User).where(User.id == job.user_id)).scalar_one()

        # ── QUEUED ────────────────────────────────────────────────────────────
        _set_status(db, job, "queued", started_at=_now())
        _publish_log(r, job_id, "info", f"Job queued — {config.name} ({config.county}, {config.state})")

        # ── PROBING ───────────────────────────────────────────────────────────
        _set_status(db, job, "probing")
        _publish_log(r, job_id, "info", "Probing county portal...")

        try:
            scraper_class = get_scraper_class(config.county, config.state, config.record_type)
        except UnsupportedCountyError as exc:
            _fail_job(db, job, r, job_id, str(exc))
            return

        # ── SCRAPING ──────────────────────────────────────────────────────────
        _set_status(db, job, "scraping")
        record_label = config.record_type.replace("_", " ").title()
        _publish_log(r, job_id, "success", f"Starting scrape — {record_label} records")

        schedule = config.schedule or {}
        date_from, date_to = _resolve_date_range(schedule)
        _publish_log(r, job_id, "info", f"Date range: {date_from} → {date_to}")

        _last_phase = [None]  # mutable for closure

        def _on_progress(page_current, page_total, record_count, phase="scraping"):
            """Called by the scraper after each page — updates the DB in real time."""
            job.page_current = page_current
            job.page_total = page_total
            job.record_count = record_count
            try:
                db.commit()
            except Exception:
                # DB connection may have gone stale during long scrape — reconnect
                try:
                    db.rollback()
                    db.commit()
                except Exception:
                    _logger.warning("Progress commit failed — will retry on next update")

            # Log phase transitions so the frontend shows what's happening
            if phase != _last_phase[0]:
                _last_phase[0] = phase
                if phase == "parcel_lookup":
                    _publish_log(r, job_id, "info", f"Looking up parcel IDs from detail pages ({page_total} records)...")
                elif phase == "enriching":
                    _publish_log(r, job_id, "info", f"Looking up addresses for {page_total} parcels...")

        _publish_log(r, job_id, "info", "Connecting to county portal...")
        # Update progress label so the live page shows activity during captcha solve
        job.progress_label = "Connecting to portal..."
        try:
            db.commit()
        except Exception:
            try: db.rollback(); db.commit()
            except Exception: pass

        try:
            records = asyncio.run(_run_scraper(scraper_class, date_from, date_to, r, job_id, _on_progress))
        except Exception:
            _logger.exception("Scraper error for job %s", job_id)
            # Reconnect DB session if it went stale during long scrape
            try:
                db.rollback()
            except Exception:
                pass
            _fail_job(db, job, r, job_id, "Scraper encountered an error — our team has been notified.")
            return

        _publish_log(r, job_id, "success", f"Scrape complete — {len(records)} records found")

        # ── Cap records to user's remaining plan quota ────────────────────────
        if user.records_limit != -1:
            remaining = max(0, user.records_limit - (user.records_used or 0))
            if remaining < len(records):
                _publish_log(
                    r, job_id, "warning",
                    f"Plan limit: saving {remaining} of {len(records)} records. Upgrade for more."
                )
                records = records[:remaining]

        # ── ENRICHING ─────────────────────────────────────────────────────────
        _set_status(db, job, "enriching", record_count=len(records))
        _publish_log(r, job_id, "info", "Saving records to database...")

        # Bulk insert results (truncate fields to fit DB column limits)
        def _trunc(val: str | None, max_len: int) -> str | None:
            return val[:max_len] if val and len(val) > max_len else val

        import uuid as _uuid

        # Bulk insert using execute + multi-row VALUES (much faster than db.add loop)
        from sqlalchemy import insert as sa_insert

        batch_size = 1000
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            rows = [
                {
                    "id": str(_uuid.uuid4()),
                    "job_id": job_id,
                    "user_id": job.user_id,
                    "date_recorded": _trunc(rec.date_recorded, 32),
                    "party_name": _trunc(rec.party_name, 512),
                    "heirs": rec.heirs,
                    "legal_description": rec.legal_description,
                    "parcel_id": _trunc(rec.parcel_id, 64),
                    "property_address": _trunc(rec.property_address, 512),
                    "mailing_address": _trunc(rec.mailing_address, 512),
                    "enrichment_data": rec.enrichment_data or {},
                    "raw_html_hash": rec.raw_html_hash,
                }
                for rec in batch
            ]
            db.execute(sa_insert(Result), rows)
            db.commit()

        _publish_log(r, job_id, "success", f"{len(records)} records saved")

        # ── EXPORT ────────────────────────────────────────────────────────────
        deliver_config = config.deliver or {}
        fmt = deliver_config.get("format", "csv")

        _publish_log(r, job_id, "info", f"Building {fmt.upper()} export...")

        record_dicts = [r_obj.to_dict() for r_obj in records]
        exporter = DataExporter()
        local_file = exporter.export(record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt)

        object_key = f"exports/{job.user_id}/{job_id}/leads.{local_file.suffix.lstrip('.')}"
        try:
            exporter.upload_to_r2(local_file, object_key)
            _publish_log(r, job_id, "success", "Export uploaded to cloud storage")
        except Exception as upload_exc:
            _logger.warning("R2 upload failed (non-fatal): %s", upload_exc)
            _publish_log(r, job_id, "warning", "Cloud upload unavailable — export saved locally")
            object_key = None  # No cloud export available
        finally:
            local_file.unlink(missing_ok=True)

        # Atomic update of monthly record usage
        from sqlalchemy import update as sa_update
        db.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(records_used=User.records_used + len(records))
        )
        db.commit()
        db.refresh(user)

        if user.records_limit != -1 and user.records_used > user.records_limit:
            overage = user.records_used - user.records_limit
            _publish_log(r, job_id, "warning", f"Plan limit exceeded by {overage} records. Upgrade to keep scraping.")

        # ── INLINE ENRICHMENT (BEFORE marking done) ──────────────────────────
        _publish_log(r, job_id, "info", "Looking up property and mailing addresses...")
        try:
            _run_inline_enrichment(db, job, r, job_id, config)
        except Exception as exc:
            _logger.warning("Inline enrichment error: %s", str(exc)[:80])
        _publish_log(r, job_id, "success", f"Enrichment complete — addresses added")

        # Re-export CSV with enriched data
        try:
            refreshed = db.execute(
                select(Result).where(Result.job_id == job_id)
            ).scalars().all()
            record_dicts = [
                {c: getattr(res, c) for c in ["date_recorded", "party_name", "heirs", "parcel_id",
                                               "property_address", "mailing_address", "legal_description"]}
                for res in refreshed
            ]
            local_file = exporter.export(record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt)
            if object_key:
                exporter.upload_to_r2(local_file, object_key)
                _logger.info("Re-exported CSV with enriched data")
        except Exception as exc:
            _logger.warning("CSV re-export failed: %s", str(exc)[:60])

        # ── NOW mark done (after enrichment + re-export) ────────────────────
        _set_status(
            db, job, "done",
            finished_at=_now(),
            record_count=len(records),
            export_key=object_key,
        )
        _publish_log(r, job_id, "success", f"Job complete — {len(records)} records ready")
        r.publish(f"job_logs:{job_id}", json.dumps({"type": "done", "record_count": len(records)}))

        # ── EMAIL DELIVERY ─────────────────────────────────────────────────────
        emails = deliver_config.get("emails", [])
        if emails and object_key:
            try:
                download_url = exporter.get_download_url(object_key, expires_in=172800)  # 48hr
                deliver_job_results(
                    job_id=job_id,
                    scraper_name=config.name,
                    record_count=len(records),
                    download_url=download_url,
                    recipient_emails=emails,
                    fmt=fmt,
                )
            except Exception as email_exc:
                _logger.warning("Email delivery failed (non-fatal): %s", email_exc)
                _publish_log(r, job_id, "warning", "Email delivery unavailable")


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _run_scraper(scraper_class, date_from: str, date_to: str, r, job_id: str, on_progress=None):
    """Run the async scraper and stream progress logs back to Redis."""
    async with scraper_class() as scraper:
        if on_progress:
            scraper.on_progress = on_progress
        records = await scraper.scrape(date_from, date_to)

        # Log AI usage if this was an AI-powered scrape
        if hasattr(scraper, "ai_cost") and scraper.ai_cost > 0:
            tokens = scraper.ai_tokens
            _publish_log(
                r, job_id, "info",
                f"AI usage: ${scraper.ai_cost:.4f} "
                f"({tokens['input_tokens']} input + {tokens['output_tokens']} output tokens)",
            )

    return records


def _run_inline_enrichment(db, job, r, job_id: str, config) -> None:
    """Run GIS + King County enrichment inline (before job marks done)."""
    from sqlalchemy import select as sa_select
    from src.db.models import Result

    all_results = db.execute(
        sa_select(Result).where(Result.job_id == job_id)
    ).scalars().all()

    # GIS batch enrichment for property addresses
    results_need_addr = [
        res for res in all_results
        if res.parcel_id and len(res.parcel_id.strip()) >= 6
        and (not res.property_address or res.property_address == "(enrichment unavailable)")
    ]
    if results_need_addr:
        _publish_log(r, job_id, "info", f"Looking up {len(results_need_addr)} property addresses...")
        from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis
        parcel_map: dict[str, list] = {}
        for res in results_need_addr:
            pid = res.parcel_id.strip()
            if pid not in parcel_map:
                parcel_map[pid] = []
            parcel_map[pid].append(res)
        gis_results = batch_enrich_parcels_gis(list(parcel_map.keys()), config.county, config.state)
        for pid, gis_data in gis_results.items():
            if not gis_data.get("property_address"):
                continue
            for res in parcel_map.get(pid, []):
                res.property_address = gis_data["property_address"]
                res.mailing_address = gis_data.get("mailing_address") or res.mailing_address
        try:
            db.commit()
        except Exception:
            db.rollback()
            db.commit()

    # King County: eRealProperty + Tax Bill for property + mailing
    if config.county.lower() == "king" and config.state.upper() == "WA":
        needs = [
            res for res in all_results
            if res.parcel_id and len(res.parcel_id.strip()) >= 6
            and not res.mailing_address
        ]
        if needs:
            _publish_log(r, job_id, "info", f"Looking up {len(needs)} mailing addresses...")
            from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county
            pids = list({res.parcel_id.strip() for res in needs})
            pid_map: dict[str, list] = {}
            for res in needs:
                pid = res.parcel_id.strip()
                if pid not in pid_map:
                    pid_map[pid] = []
                pid_map[pid].append(res)
            enriched = asyncio.run(batch_enrich_king_county(pids))
            for pid, data in enriched.items():
                prop = data.get("property_address")
                mail = data.get("mailing_address")
                for res in pid_map.get(pid, []):
                    if prop and not res.property_address:
                        res.property_address = prop
                    if mail:
                        res.mailing_address = mail
            try:
                db.commit()
            except Exception:
                db.rollback()
                db.commit()
            found = sum(1 for d in enriched.values() if d.get("mailing_address"))
            _publish_log(r, job_id, "info", f"Found {found}/{len(pids)} mailing addresses")


def _fail_job(db, job, r, job_id: str, reason: str) -> None:
    """Transition job to FAILED with a human-readable error message."""
    try:
        db.rollback()  # Clear any pending rollback from previous errors
    except Exception:
        pass
    _set_status(db, job, "failed", finished_at=_now(), error_message=reason)
    _publish_log(r, job_id, "error", reason)
    r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
    _logger.error("Job %s failed: %s", job_id, reason)


def _to_mmddyyyy(date_str: str) -> str:
    """Convert any date string to MM/DD/YYYY format for county portals.

    Handles: YYYY-MM-DD (ISO), MM/DD/YYYY (already correct), M/D/YYYY.
    """
    date_str = date_str.strip()
    # Already MM/DD/YYYY
    if len(date_str) == 10 and date_str[2] == "/" and date_str[5] == "/":
        return date_str
    # ISO format: YYYY-MM-DD
    if len(date_str) >= 10 and date_str[4] == "-":
        parts = date_str[:10].split("-")
        if len(parts) == 3:
            return f"{parts[1]}/{parts[2]}/{parts[0]}"
    # Fallback: try parsing with datetime
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return _dt.strptime(date_str[:10], fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return date_str  # Return as-is if nothing works


def _resolve_date_range(schedule: dict) -> tuple[str, str]:
    """Compute date_from and date_to from a scraper's schedule config."""
    from datetime import timedelta

    today = datetime.now(UTC).date()
    # Support both key names: schema uses "date_range_mode", legacy used "range_mode"
    range_mode = schedule.get("date_range_mode") or schedule.get("range_mode", "rolling_90")

    if range_mode == "custom":
        date_from = schedule.get("date_from", "")
        date_to = schedule.get("date_to", "")
        if date_from and date_to:
            # Normalize to MM/DD/YYYY — frontend may send YYYY-MM-DD (ISO)
            return _to_mmddyyyy(date_from), _to_mmddyyyy(date_to)
        # Fall through to rolling_90 if custom dates are missing

    if range_mode == "since_last_run":
        # Fallback to rolling_90 until last_run tracking is implemented
        date_from = today - timedelta(days=90)
    elif range_mode == "rolling_30":
        date_from = today - timedelta(days=30)
    elif range_mode == "rolling_7":
        date_from = today - timedelta(days=7)
    else:
        date_from = today - timedelta(days=90)

    return date_from.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y")


# ─── Enrichment task (runs separately from scraping) ─────────────────────────


@app.task(
    name="src.workers.tasks.enrich_job_results",
    bind=True,
    max_retries=1,
    default_retry_delay=60,
    acks_late=True,
    soft_time_limit=2700,  # 45 min (mailing enrichment for 500+ parcels)
    time_limit=3000,       # 50 min
)
def enrich_job_results(self, job_id: str) -> None:
    """DEPRECATED — enrichment now runs inline in run_scrape_job.

    This task is kept as a no-op so old queued messages don't crash.
    """
    _logger.info("enrich_job_results called for %s — skipping (enrichment is now inline)", job_id)
    return
    from sqlalchemy import select

    from src.db.models import Job, Result, ScraperConfig
    from src.db.session import SyncSessionLocal

    r = _redis()

    with SyncSessionLocal() as db:
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if job is None or job.status != "done":
            _logger.info("Enrichment skipped for job %s (status=%s)", job_id, job.status if job else "not found")
            return

        config = db.execute(
            select(ScraperConfig).where(ScraperConfig.id == job.scraper_config_id)
        ).scalar_one_or_none()
        if config is None:
            return

        # Step 1: Find ALL results (for parcel ID fetching from ARMS detail pages)
        all_results = db.execute(
            select(Result).where(Result.job_id == job_id)
        ).scalars().all()

        # Step 2: Fetch parcel IDs from ARMS detail pages for records missing them
        needs_parcel = [
            res for res in all_results
            if not res.parcel_id
            and res.enrichment_data
            and isinstance(res.enrichment_data, dict)
            and res.enrichment_data.get("instrument_number")
        ]

        if needs_parcel:
            _logger.info("Fetching parcel IDs from ARMS detail pages for %d records", len(needs_parcel))
            _publish_log(r, job_id, "info", f"Fetching parcel IDs from detail pages ({len(needs_parcel)} records)...")
            parcel_count = asyncio.run(_fetch_parcel_ids_from_arms(needs_parcel, db, r, job_id))
            _publish_log(r, job_id, "info", f"Found {parcel_count} parcel IDs from detail pages")

        # Step 3: GIS enrichment for records with parcel_id but no address
        results = [
            res for res in all_results
            if res.parcel_id
            and len(res.parcel_id.strip()) >= 10
            and (not res.property_address or res.property_address == "(enrichment unavailable)")
        ]

        if not results:
            _logger.info("No results need address enrichment for job %s", job_id)
            _publish_log(r, job_id, "info", "No records with parcel IDs to enrich")
            return

        _logger.info("GIS enriching %d results for job %s", len(results), job_id)
        _publish_log(r, job_id, "info", f"Enriching {len(results)} records via batch GIS...")

        # Batch GIS enrichment — 50 parcels per API call instead of 1
        from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis

        # Build parcel_id -> result mapping
        parcel_map: dict[str, list] = {}
        for result in results:
            pid = result.parcel_id.strip()
            if pid not in parcel_map:
                parcel_map[pid] = []
            parcel_map[pid].append(result)

        all_parcels = list(parcel_map.keys())
        _logger.info("Batch GIS: %d unique parcels from %d records", len(all_parcels), len(results))

        # Single batch call handles chunking internally (50 per API call)
        gis_results = batch_enrich_parcels_gis(all_parcels, config.county, config.state)

        # Apply results to DB records
        enriched_count = 0
        for pid, gis_data in gis_results.items():
            if not gis_data.get("property_address"):
                continue
            for result in parcel_map.get(pid, []):
                result.property_address = gis_data["property_address"]
                result.mailing_address = gis_data.get("mailing_address") or result.mailing_address
                result.enrichment_data = gis_data
                enriched_count += 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            db.commit()
        _publish_log(r, job_id, "info", f"GIS enrichment: {enriched_count}/{len(results)} property addresses found")
        _logger.info("GIS enrichment for job %s: %d/%d enriched", job_id, enriched_count, len(results))

        # Step 4: King County — get property + mailing from payment.kingcounty.gov
        # This gives both addresses in one lookup (better than GIS for mailing)
        if config.county.lower() == "king" and config.state.upper() == "WA":
            needs_address = [
                res for res in all_results
                if res.parcel_id
                and len(res.parcel_id.strip()) >= 6
                and (not res.mailing_address)
            ]
            if needs_address:
                _logger.info("King County address enrichment: %d records", len(needs_address))
                _publish_log(r, job_id, "info", f"Looking up addresses for {len(needs_address)} records...")
                addr_count = asyncio.run(
                    _enrich_king_county_mailing(needs_address, db, r, job_id)
                )
                _publish_log(r, job_id, "info", f"Addresses found: {addr_count}/{len(needs_address)}")

        _publish_log(r, job_id, "success", f"Enrichment complete — {enriched_count}/{len(results)} addresses found")
        _logger.info("Enrichment complete for job %s: %d/%d enriched", job_id, enriched_count, len(results))

        # Re-export CSV with enriched data (original export happened before enrichment)
        try:
            from src.utils.data_exporter import DataExporter
            refreshed = db.execute(
                select(Result).where(Result.job_id == job_id)
            ).scalars().all()
            record_dicts = [
                {c: getattr(r, c) for c in ["date_recorded", "party_name", "heirs", "parcel_id",
                                             "property_address", "mailing_address", "legal_description"]}
                for r in refreshed
            ]
            exporter = DataExporter()
            local_file = exporter.export(record_dicts, filename=f"job_{job_id[:8]}", fmt="csv")
            object_key = f"exports/{job.user_id}/{job_id}/leads.csv"
            exporter.upload_to_r2(local_file, object_key)
            _logger.info("Re-exported CSV with enriched data: %d records", len(record_dicts))
        except Exception as exc:
            _logger.warning("CSV re-export failed: %s", str(exc)[:80])


async def _enrich_king_county_mailing(results, db, r, job_id: str) -> int:
    """Look up mailing addresses from payment.kingcounty.gov for King County records."""
    from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county

    parcel_ids = list({res.parcel_id.strip() for res in results if res.parcel_id})
    if not parcel_ids:
        return 0

    # Build parcel_id -> results mapping
    parcel_map: dict[str, list] = {}
    for res in results:
        pid = res.parcel_id.strip()
        if pid not in parcel_map:
            parcel_map[pid] = []
        parcel_map[pid].append(res)

    enriched = await batch_enrich_king_county(parcel_ids)

    count = 0
    for pid, data in enriched.items():
        prop = data.get("property_address")
        mailing = data.get("mailing_address")
        if not mailing and not prop:
            continue
        for res in parcel_map.get(pid, []):
            if prop and not res.property_address:
                res.property_address = prop
            if mailing:
                res.mailing_address = mailing
            count += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        db.commit()

    return count


async def _fetch_parcel_ids_from_arms(results, db, r, job_id: str) -> int:
    """Open ARMS in a separate browser, navigate to each record's detail page,
    and extract the real parcel ID from the Legal Description tab.

    Processes records page by page using the instrument number dropdown.
    """
    from src.api.middleware.security import add_scrape_domain
    from src.scrapers.base_scraper import BridgeScraper

    add_scrape_domain("armsweb.co.pierce.wa.us")

    found = 0
    async with BridgeScraper() as scraper:
        # Accept disclaimer + search for the same records
        await scraper.navigate("https://armsweb.co.pierce.wa.us/")
        try:
            accept = scraper.page.locator("a:has-text('Click here to acknowledge')")
            await accept.wait_for(timeout=5_000)
            await accept.click()
            await scraper.page.wait_for_load_state("load")
        except Exception:
            pass

        await scraper.navigate("https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx")
        await scraper.page.wait_for_timeout(1_000)

        # Check PROBATE + fill dates + search
        probate_cb = scraper.page.get_by_role("checkbox", name="PROBATE")
        try:
            await probate_cb.scroll_into_view_if_needed(timeout=10_000)
            await probate_cb.check(timeout=5_000)
        except Exception:
            await scraper.page.evaluate("""() => {
                const cbs = document.querySelectorAll('input[type="checkbox"]');
                for (const cb of cbs) {
                    const sib = cb.nextSibling;
                    if (sib && sib.textContent && sib.textContent.trim() === 'PROBATE') {
                        cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles: true})); break;
                    }
                }
            }""")

        from datetime import UTC, timedelta
        today = datetime.now(UTC).date()
        date_from = (today - timedelta(days=90)).strftime("%m/%d/%Y")
        date_to = today.strftime("%m/%d/%Y")

        await scraper.page.evaluate("""([df, dt]) => {
            const inputs = document.querySelectorAll('input[title="mm/dd/yyyy"]');
            if (inputs.length >= 2) {
                inputs[0].value = df; inputs[0].dispatchEvent(new Event('change', {bubbles: true}));
                inputs[1].value = dt; inputs[1].dispatchEvent(new Event('change', {bubbles: true}));
            }
        }""", [date_from, date_to])

        search_btn = scraper.page.get_by_role("button", name="Search", exact=True).first
        await search_btn.scroll_into_view_if_needed(timeout=5_000)
        await search_btn.click(timeout=10_000)
        await scraper.page.wait_for_load_state("load")
        await scraper.page.wait_for_timeout(2_000)

        # Build a map of instrument_number → result for quick lookup
        inst_map = {}
        for res in results:
            inst = res.enrichment_data.get("instrument_number") if isinstance(res.enrichment_data, dict) else None
            if inst:
                inst_map[inst] = res

        # Click the first visible instrument link on the ARMS results page
        first_visible = await scraper.page.evaluate(r"""() => {
            const links = document.querySelectorAll('a[href*="javascript"]');
            for (const a of links) {
                const text = a.textContent.trim();
                if (/^\d{10,12}$/.test(text)) return text;
            }
            return null;
        }""")
        if not first_visible:
            _logger.warning("No instrument links found on ARMS results page")
            return 0

        try:
            await scraper.page.locator(f"text={first_visible}").first.click(timeout=10_000)
            await scraper.page.wait_for_load_state("load")
            await scraper.page.wait_for_timeout(1_000)
        except Exception:
            _logger.warning("Could not click first instrument %s", first_visible)
            return 0

        # Process ALL pages of the dropdown
        processed_instruments = set()
        for page_idx in range(10):  # Max 10 pages
            options = await scraper.page.evaluate("""() => {
                const sel = document.querySelector('select');
                if (!sel) return [];
                return Array.from(sel.options).map(o => o.value.trim());
            }""")

            if not options or all(o in processed_instruments for o in options):
                break

            _logger.info("Detail page %d: %d instruments in dropdown", page_idx + 1, len(options))

            for inst_num in options:
                if inst_num in processed_instruments:
                    continue
                processed_instruments.add(inst_num)

                if inst_num not in inst_map:
                    continue

            try:
                # Select this instrument
                dropdown = scraper.page.locator("select").first
                await dropdown.select_option(value=inst_num, timeout=5_000)
                await scraper.page.wait_for_load_state("load")
                await scraper.page.wait_for_timeout(500)

                # Click Legal Description tab
                legal_tab = scraper.page.locator("text=Legal Description").first
                if await legal_tab.count() > 0:
                    await legal_tab.click(timeout=3_000)
                    await scraper.page.wait_for_timeout(500)

                # Extract parcel ID
                parcel_id = await scraper.page.evaluate("""() => {
                    const cells = document.querySelectorAll('td');
                    for (let i = 0; i < cells.length; i++) {
                        if (cells[i].textContent.trim() === 'Parcel Id:' && cells[i+1]) {
                            return cells[i+1].textContent.trim();
                        }
                    }
                    return null;
                }""")

                if parcel_id and parcel_id.strip():
                    result = inst_map[inst_num]
                    result.parcel_id = parcel_id.strip()
                    db.commit()
                    found += 1

                    if found <= 5 or found % 20 == 0:
                        _publish_log(r, job_id, "info", f"  {inst_num} → parcel {parcel_id.strip()}")

            except Exception as exc:
                _logger.warning("Detail failed for %s: %s", inst_num, str(exc)[:40])

            # Navigate to next page of results (to get more instruments in dropdown)
            try:
                # Go back to results list first
                back_link = scraper.page.locator("text=Back to Results").first
                if await back_link.count() > 0:
                    await back_link.click(timeout=5_000)
                    await scraper.page.wait_for_load_state("load")
                    await scraper.page.wait_for_timeout(1_000)

                # Click Next page button
                next_btn = scraper.page.get_by_role("button", name="Next", exact=True).first
                if await next_btn.count() > 0:
                    is_disabled = await next_btn.get_attribute("disabled")
                    if not is_disabled:
                        await next_btn.click(timeout=5_000)
                        await scraper.page.wait_for_load_state("load")
                        await scraper.page.wait_for_timeout(1_000)

                        # Click first instrument on new page to re-enter detail view
                        first_on_page = await scraper.page.evaluate(r"""() => {
                            const links = document.querySelectorAll('a[href*="javascript"]');
                            for (const a of links) {
                                const text = a.textContent.trim();
                                if (/^\d{10,12}$/.test(text)) return text;
                            }
                            return null;
                        }""")
                        if first_on_page:
                            await scraper.page.locator(f"text={first_on_page}").first.click(timeout=5_000)
                            await scraper.page.wait_for_load_state("load")
                            await scraper.page.wait_for_timeout(500)
                        else:
                            break
                    else:
                        break  # No more pages
                else:
                    break
            except Exception:
                break  # Navigation failed, stop paginating

    return found
