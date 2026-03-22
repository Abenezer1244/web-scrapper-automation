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
        _publish_log(r, job_id, "success", f"Starting scrape — {config.record_type} records")

        schedule = config.schedule or {}
        date_from, date_to = _resolve_date_range(schedule)
        _publish_log(r, job_id, "info", f"Date range: {date_from} → {date_to}")

        try:
            records = asyncio.run(_run_scraper(scraper_class, date_from, date_to, r, job_id))
        except Exception:
            _logger.exception("Scraper error for job %s", job_id)
            _fail_job(db, job, r, job_id, "Scraper encountered an error — our team has been notified.")
            return

        _publish_log(r, job_id, "success", f"Scrape complete — {len(records)} records found")

        # ── ENRICHING ─────────────────────────────────────────────────────────
        _set_status(db, job, "enriching", record_count=len(records))
        _publish_log(r, job_id, "info", "Saving records to database...")

        # Bulk insert results (truncate fields to fit DB column limits)
        def _trunc(val: str | None, max_len: int) -> str | None:
            return val[:max_len] if val and len(val) > max_len else val

        import uuid as _uuid

        # Batch insert records (500 at a time to avoid memory/timeout issues)
        batch_size = 500
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            for record in batch:
                db.add(Result(
                    id=str(_uuid.uuid4()),
                    job_id=job_id,
                    user_id=job.user_id,
                    date_recorded=_trunc(record.date_recorded, 32),
                    party_name=_trunc(record.party_name, 512),
                    heirs=record.heirs,
                    legal_description=record.legal_description,
                    parcel_id=_trunc(record.parcel_id, 64),
                    property_address=_trunc(record.property_address, 512),
                    mailing_address=_trunc(record.mailing_address, 512),
                    enrichment_data=record.enrichment_data or {},
                    raw_html_hash=record.raw_html_hash,
                ))
            db.commit()
            _publish_log(r, job_id, "info", f"Saved batch {i//batch_size + 1} ({min(i+batch_size, len(records))}/{len(records)} records)")

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

        # ── DONE ─────────────────────────────────────────────────────────────
        _set_status(
            db, job, "done",
            finished_at=_now(),
            record_count=len(records),
            export_key=object_key,
        )

        # Update user's monthly record usage
        user.records_used = (user.records_used or 0) + len(records)
        db.commit()

        _publish_log(r, job_id, "success", f"Job complete — {len(records)} records ready")
        r.publish(f"job_logs:{job_id}", json.dumps({"type": "done", "record_count": len(records)}))

        # ── TRIGGER ENRICHMENT (separate task) ────────────────────────────────
        # Enrichment runs in a separate Celery task to avoid running two
        # Playwright browsers in the same worker (memory exhaustion).
        enrich_job_results.delay(job_id)

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

async def _run_scraper(scraper_class, date_from: str, date_to: str, r, job_id: str):
    """Run the async scraper and stream progress logs back to Redis."""
    async with scraper_class() as scraper:
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


def _fail_job(db, job, r, job_id: str, reason: str) -> None:
    """Transition job to FAILED with a human-readable error message."""
    _set_status(db, job, "failed", finished_at=_now(), error_message=reason)
    _publish_log(r, job_id, "error", reason)
    r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
    _logger.error("Job %s failed: %s", job_id, reason)


def _resolve_date_range(schedule: dict) -> tuple[str, str]:
    """Compute date_from and date_to from a scraper's schedule config."""
    from datetime import timedelta

    today = datetime.now(UTC).date()
    range_mode = schedule.get("range_mode", "rolling_90")

    if range_mode == "custom":
        return schedule.get("date_from", ""), schedule.get("date_to", "")
    if range_mode == "since_last_run":
        # Fallback to rolling_90 until last_run tracking is implemented
        date_from = today - timedelta(days=90)
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
    soft_time_limit=900,   # 15 min
    time_limit=960,        # 16 min
)
def enrich_job_results(self, job_id: str) -> None:
    """Enrich all results in a completed job with property/mailing addresses.

    Runs as a separate Celery task after scraping completes.
    Uses its own Playwright browser (no conflict with the scraper).
    Only enriches records that have a parcel_id but no property_address.
    """
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

        # Step 3: Enrich records that have parcel_id but no address
        results = [
            res for res in all_results
            if res.parcel_id
            and res.parcel_id.strip()
            and (not res.property_address or res.property_address == "(enrichment unavailable)")
        ]

        if not results:
            _logger.info("No results need address enrichment for job %s", job_id)
            _publish_log(r, job_id, "info", "No records with parcel IDs to enrich")
            return

        _logger.info("Enriching %d results for job %s", len(results), job_id)
        _publish_log(r, job_id, "info", f"Enriching {len(results)} records with property addresses...")

        # Run async enrichment
        enriched_count = asyncio.run(
            _run_enrichment(results, config.county, config.state, db, r, job_id)
        )

        _publish_log(r, job_id, "success", f"Enrichment complete — {enriched_count} addresses found")
        _logger.info("Enrichment complete for job %s: %d/%d enriched", job_id, enriched_count, len(results))


async def _run_enrichment(results, county: str, state: str, db, r, job_id: str) -> int:
    """Run async enrichment for a batch of results."""
    from src.scrapers.base_scraper import BridgeScraper
    from src.scrapers.enrichment.captcha import solve_recaptcha

    enriched = 0
    sitekey = "6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT"
    page_url = "https://atip.piercecountywa.gov/app/parcelSearch"

    # Solve CAPTCHA once
    from src.config import settings
    if not settings.CAPTCHA_ENABLED or not settings.CAPTCHA_API_KEY:
        _logger.warning("CAPTCHA not configured — skipping enrichment")
        return 0

    token = await solve_recaptcha(page_url, sitekey)
    if not token:
        _publish_log(r, job_id, "warning", "CAPTCHA solving failed — enrichment skipped")
        return 0

    # Open ONE browser for all lookups
    async with BridgeScraper() as scraper:
        await scraper.navigate(page_url)
        await scraper.page.wait_for_timeout(2_000)

        for i, result in enumerate(results):
            parcel_id = result.parcel_id.strip()

            # Refresh CAPTCHA token if needed (every ~25 lookups)
            if i > 0 and i % 20 == 0:
                token = await solve_recaptcha(page_url, sitekey)
                if not token:
                    _logger.warning("CAPTCHA refresh failed at record %d", i)
                    break

            # Call ATIP API from browser context
            try:
                api_result = await scraper.page.evaluate("""
                    async (args) => {
                        const [pid, tok] = args;
                        try {
                            const r = await fetch('/api/parcelSearch?value=' + pid, {
                                headers: {'Accept':'application/json','recaptcha-response':tok}
                            });
                            if (r.status !== 200) return null;
                            const data = await r.json();
                            if (!data || !data.length) return null;
                            return {
                                address: (data[0].line1 || '').trim() || null,
                                name: (data[0].name || '').trim() || null
                            };
                        } catch(e) { return null; }
                    }
                """, [parcel_id, token])

                if api_result and api_result.get("address"):
                    address = api_result["address"]
                    result.property_address = address
                    result.mailing_address = address  # Owner-occupied assumption
                    result.enrichment_data = {"owner": api_result.get("name"), "source": "atip"}
                    db.commit()
                    enriched += 1

                    if enriched <= 5 or enriched % 10 == 0:
                        _publish_log(r, job_id, "info", f"  {parcel_id} → {address}")

            except Exception as exc:
                _logger.warning("Enrichment failed for %s: %s", parcel_id, str(exc)[:60])

            # Polite delay
            await scraper.polite_delay()

    return enriched


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
