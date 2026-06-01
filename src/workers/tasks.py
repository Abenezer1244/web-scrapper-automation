"""Celery task: full scrape job lifecycle.

State machine:
    PENDING → QUEUED → PROBING → SCRAPING → ENRICHING → DONE
                                                        → FAILED
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, Unpack

import redis as sync_redis

if TYPE_CHECKING:
    from src.scrapers.base_scraper import ProgressCallback

from src.config import settings
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.task")

# Delivery download links: prefer a revocable app download-token URL (honors
# logout-all + the jti blacklist, scoped to user+job) over a raw 48h R2
# presigned bearer URL sitting in an inbox. Falls back to the presigned URL
# only until settings.API_BASE_URL is configured, so delivery never breaks.
_DELIVERY_TOKEN_TTL = 172800  # 48h — matches the prior presigned URL lifetime


def _delivery_download_url(job_id: str, user_id, object_key: str, exporter) -> str:
    if settings.API_BASE_URL:
        from src.api.download_tokens import mint_download_token
        token = mint_download_token(str(user_id), job_id, ttl_seconds=_DELIVERY_TOKEN_TTL)
        return f"{settings.API_BASE_URL.rstrip('/')}/jobs/{job_id}/download?token={token}"
    return exporter.get_download_url(object_key, expires_in=_DELIVERY_TOKEN_TTL)


class JobUpdateFields(TypedDict, total=False):
    """Fields _set_status() may set on a Job ORM row alongside `status`.

    Every key is a column on src.db.models.Job; total=False because each
    callsite passes a different subset (e.g. just `started_at` on
    transition into "queued", but `finished_at` + `record_count` +
    `export_key` on transition into "done"). Using TypedDict + Unpack
    means a typo like `started=` (instead of `started_at`) is now a
    static type error rather than a silent setattr no-op.
    """

    started_at: datetime
    finished_at: datetime
    record_count: int
    page_current: int
    page_total: int
    error_message: str
    export_key: str


def _now() -> datetime:
    return datetime.now(UTC)


def _redis() -> sync_redis.Redis:
    return sync_redis.from_url(settings.REDIS_URL, decode_responses=True)


def _publish_log(r: sync_redis.Redis, job_id: str, level: str, message: str, db=None) -> None:
    """Publish a log line to Redis Pub/Sub and persist it to the DB.

    Pass an existing ``db`` session to avoid opening a new connection per log line.
    """
    import uuid

    from src.db.models import JobLog

    payload = {
        "id": str(uuid.uuid4()),
        "level": level,
        "message": message,
        "created_at": _now().isoformat(),
        "type": "log",
    }
    r.publish(f"job_logs:{job_id}", json.dumps(payload))

    # Persist to DB for SSE replay
    if db is not None:
        db.add(JobLog(
            id=payload["id"],
            job_id=job_id,
            level=level,
            message=message,
        ))
        db.commit()
    else:
        # Fallback path — no session was passed in, so we open a
        # system-level session. JobLog writes are keyed on job_id and
        # the caller has already verified ownership upstream; the
        # table's RLS policy (job_logs_via_job) filters reads via a
        # subquery on jobs.user_id, not writes.
        from src.db.session import system_sync_session
        with system_sync_session() as _db:
            _db.add(JobLog(
                id=payload["id"],
                job_id=job_id,
                level=level,
                message=message,
            ))
            _db.commit()


def _set_status(db, job, status: str, **kwargs: Unpack[JobUpdateFields]) -> None:
    """Update job status and any extra fields, then commit.

    `kwargs` keys are constrained by the JobUpdateFields TypedDict so a
    typo like `started=...` (instead of `started_at=...`) fails type
    checking instead of silently doing nothing.
    """
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
    from sqlalchemy import func, select

    from src.api.middleware.security import register_connector_domains_from_db
    from src.db.models import Job, Result, ScraperConfig, User
    from src.db.session import rls_sync_session, system_sync_session
    from src.scrapers.registry import UnsupportedCountyError, get_scraper_class
    from src.utils.data_exporter import DataExporter
    from src.workers.delivery import deliver_job_results

    # Refresh the in-process SSRF allowlist from the connectors table before
    # scraping. A connector added through POST /scrapers/connectors after this
    # worker booted would otherwise still be missing from the frozenset that
    # validate_scraping_target() checks. Idempotent and cheap.
    register_connector_domains_from_db()

    r = _redis()

    # ── Bootstrap: look up user_id for this job_id without RLS ──────────────
    # We need the user_id BEFORE we can enter the RLS-scoped session, and
    # the Celery task only receives job_id. This bootstrap query is a
    # legitimate system operation — the Celery task was dispatched by the
    # API which already authorized this user, and the worker's role is to
    # act on their behalf. Loading the user_id by job_id cannot leak data:
    # the caller already knows the job_id they're asking about.
    with system_sync_session() as _boot:
        boot_row = _boot.execute(
            select(Job.user_id, Job.status).where(Job.id == job_id)
        ).first()
        if boot_row is None:
            _logger.error("Job %s not found — aborting", job_id)
            return
        _boot_user_id, _boot_status = boot_row
        if _boot_status == "cancelled":
            _logger.info("Job %s was cancelled before worker picked it up", job_id)
            return

    # Everything past this point runs with the RLS policies bound to
    # this job's user_id. Inserts into results, delivered_records,
    # pending_skip_trace_rows etc. are now scoped at the DB level as
    # well as the ORM level. H1 + C1 from the full-SaaS review.
    with rls_sync_session(_boot_user_id) as db:
        # ── Load job ─────────────────────────────────────────────────────────
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if job is None:
            _logger.error("Job %s disappeared between bootstrap and load", job_id)
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
        _publish_log(r, job_id, "info", f"Job queued — {config.name} ({config.county}, {config.state})", db=db)

        # ── PROBING ───────────────────────────────────────────────────────────
        _set_status(db, job, "probing")
        _publish_log(r, job_id, "info", "Probing county portal...", db=db)

        try:
            scraper_class, matched_record_type = get_scraper_class(config.county, config.state, config.record_type)
        except UnsupportedCountyError as exc:
            _fail_job(db, job, r, job_id, str(exc))
            return

        # ── SCRAPING ──────────────────────────────────────────────────────────
        _set_status(db, job, "scraping")
        record_label = config.record_type.replace("_", " ").title()
        _publish_log(r, job_id, "success", f"Starting scrape — {record_label} records", db=db)

        from typing import cast
        from src.api.schemas import ScheduleConfigDict
        schedule: ScheduleConfigDict = cast(ScheduleConfigDict, config.schedule or {})
        range_mode = schedule.get("date_range_mode") or schedule.get("range_mode", "rolling_90")  # type: ignore[call-overload]  # legacy "range_mode" alias kept for old configs
        date_from, date_to = _resolve_date_range(schedule, config_id=config.id, job_id=job_id, user_plan=user.plan)

        # Enforce per-connector max date range (e.g. Chelan single-date = 30 days max).
        # Look up the connector to get the limit.
        from src.db.models import CountyConnector
        connector = db.execute(
            select(CountyConnector).where(
                func.lower(CountyConnector.county) == config.county.lower(),
                func.upper(CountyConnector.state) == config.state.upper(),
                CountyConnector.active,
            )
        ).scalars().first()
        max_days = connector.max_date_range_days if connector else None
        if max_days:
            from datetime import timedelta as _td
            _df = datetime.strptime(date_from, "%m/%d/%Y")
            _dt = datetime.strptime(date_to, "%m/%d/%Y")
            actual_days = (_dt - _df).days
            if actual_days > max_days:
                # Trim date_from to respect the limit (keep the most recent data)
                _df = _dt - _td(days=max_days)
                date_from = _df.strftime("%m/%d/%Y")
                _publish_log(
                    r, job_id, "warning",
                    f"{config.county.title()} County supports max {max_days} days. Range trimmed to {date_from} → {date_to}.",
                    db=db,
                )

        job.date_from = date_from
        job.date_to = date_to
        db.flush()
        _publish_log(r, job_id, "info", f"Date range: {date_from} → {date_to} (mode: {range_mode})", db=db)

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
                    _publish_log(r, job_id, "info", f"Looking up parcel IDs from detail pages ({page_total} records)...", db=db)
                elif phase == "enriching":
                    _publish_log(r, job_id, "info", f"Looking up addresses for {page_total} parcels...", db=db)

        _publish_log(r, job_id, "info", "Connecting to county portal...", db=db)
        # Update progress label so the live page shows activity during captcha solve.
        # L7 (full-SaaS review): the previous "rollback() then commit()"
        # recovery dance is fragile — unpack it into explicit branches
        # so the intent is grep-friendly.
        job.progress_label = "Connecting to portal..."
        try:
            db.commit()
        except Exception as commit_exc:
            _logger.warning(
                "Failed to commit progress_label for job %s: %s",
                job_id, str(commit_exc)[:120],
            )
            try:
                db.rollback()
            except Exception:
                pass

        try:
            # Wrap scraper in a 30-minute timeout so a hung Playwright
            # session doesn't burn the full 60-min Celery soft_time_limit.
            _SCRAPE_TIMEOUT = 1800  # 30 minutes
            records = asyncio.run(
                asyncio.wait_for(
                    _run_scraper(scraper_class, date_from, date_to, r, job_id, _on_progress, record_type=matched_record_type),
                    timeout=_SCRAPE_TIMEOUT,
                )
            )
        except asyncio.TimeoutError:
            _logger.error("Scraper timed out after %ds for job %s", _SCRAPE_TIMEOUT, job_id)
            try:
                db.rollback()
            except Exception:
                pass
            _fail_job(db, job, r, job_id, f"Scraper timed out after {_SCRAPE_TIMEOUT // 60} minutes. Try a shorter date range.")
            return
        except Exception:
            _logger.exception("Scraper error for job %s", job_id)
            # Reconnect DB session if it went stale during long scrape
            try:
                db.rollback()
            except Exception:
                pass
            _fail_job(db, job, r, job_id, "Scraper encountered an error — our team has been notified.")
            return

        _publish_log(r, job_id, "success", f"Scrape complete — {len(records)} records found", db=db)

        # ── Cap records to user's remaining plan quota ────────────────────────
        if user.records_limit != -1:
            remaining = max(0, user.records_limit - (user.records_used or 0))
            if remaining < len(records):
                _publish_log(
                    r, job_id, "warning",
                    f"Plan limit: saving {remaining} of {len(records)} records. Upgrade for more.",
                    db=db)
                records = records[:remaining]

        # ── ENRICHING ─────────────────────────────────────────────────────────
        _set_status(db, job, "enriching", record_count=len(records))
        _publish_log(r, job_id, "info", "Saving records to database...", db=db)

        # Bulk insert results (truncate fields to fit DB column limits)
        def _trunc(val: str | None, max_len: int) -> str | None:
            return val[:max_len] if val and len(val) > max_len else val

        import hashlib
        import re as _re
        import uuid as _uuid

        def _compute_dedup_hash(
            parcel_id: str | None,
            property_address: str | None,
            party_name: str | None = None,
            date_recorded: str | None = None,
        ) -> str | None:
            """Sprint 6.4: canonical dedup key.

            Primary key: normalized (parcel_id, property_address).
            Fallback key: normalized (party_name, date_recorded) — used
            when records have no parcel/address (e.g. pre-foreclosure
            from AcclaimWeb). Without this fallback, records with no
            parcel and no address would never deduplicate and show as
            NEW on every scrape.
            """
            parcel = (parcel_id or "").strip().upper().replace("-", "").replace(" ", "")
            addr = (property_address or "").strip().upper()
            addr = _re.sub(r"[\.,#]", " ", addr)
            addr = _re.sub(r"\s+", " ", addr).strip()

            parcel_ok = len(parcel) >= 4 and any(c.isdigit() for c in parcel)
            addr_ok = len(addr) >= 8 and any(c.isalpha() for c in addr)

            if parcel_ok or addr_ok:
                key = f"{parcel}|{addr}"
                return hashlib.sha256(key.encode("utf-8")).hexdigest()

            # Fallback: party_name + date_recorded
            name = (party_name or "").strip().upper()
            name = _re.sub(r"\s+", " ", name).strip()
            date = (date_recorded or "").strip()
            if len(name) >= 3 and len(date) >= 6:
                key = f"NAME:{name}|DATE:{date}"
                return hashlib.sha256(key.encode("utf-8")).hexdigest()

            return None

        # Bulk insert using execute + multi-row VALUES (much faster than db.add loop)
        from sqlalchemy import insert as sa_insert

        batch_size = 1000
        total_rows_inserted = 0
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
                    # Sprint 6.4: dedup hash computed now, duplicate flag
                    # resolved in the post-insert dedup scan below
                    "dedup_hash": _compute_dedup_hash(rec.parcel_id, rec.property_address, rec.party_name, rec.date_recorded),
                    "is_duplicate": False,
                }
                for rec in batch
            ]
            db.execute(sa_insert(Result), rows)
            db.commit()
            total_rows_inserted += len(rows)

        # ── SPRINT 6.4: CROSS-JOB DEDUPLICATION ────────────────────────────
        # For each newly-inserted Result that has a dedup_hash, try to
        # INSERT into delivered_records. PostgreSQL's ON CONFLICT DO
        # NOTHING tells us which rows were successfully claimed (first
        # delivery) vs which conflicted (user has seen this lead before).
        # The conflicting rows get their Result flagged is_duplicate=true.
        _publish_log(r, job_id, "info", "Checking for duplicate leads...", db=db)
        _logger.info("Job %s: dedup step 1 — SELECT fresh rows", job_id)

        # Step 1: pull the freshly-inserted results back so we have their
        # Result.id for the first_result_id foreign key
        from sqlalchemy import text as sa_text
        fresh_rows = db.execute(
            sa_text("""
                SELECT id, dedup_hash, parcel_id, property_address
                FROM results
                WHERE job_id = :jid AND dedup_hash IS NOT NULL
            """),
            {"jid": job_id},
        ).fetchall()

        _logger.info("Job %s: dedup step 1 done — %d fresh rows", job_id, len(fresh_rows))
        dup_count = 0
        unique_count = 0
        if fresh_rows:
            _logger.info("Job %s: dedup step 2 — INSERT delivered_records", job_id)
            # Step 2: single batched upsert into delivered_records.
            # ON CONFLICT DO NOTHING is the atomic "claim first delivery"
            # primitive — the unique (user_id, dedup_hash) constraint
            # guarantees exactly one winner per lead per user.
            # RETURNING tells us which hashes were actually inserted
            # (the "first delivery" ones) so we can derive duplicates
            # via set difference.
            insert_payload = [
                {
                    "id": str(_uuid.uuid4()),
                    "user_id": str(job.user_id),
                    "dedup_hash": row.dedup_hash,
                    "first_result_id": str(row.id),
                    "first_job_id": job_id,
                    "parcel_id": row.parcel_id,
                    "property_address": row.property_address,
                }
                for row in fresh_rows
            ]
            # Batch in groups of 500 to keep the SQL statement reasonable
            claimed_hashes: set[str] = set()
            for j in range(0, len(insert_payload), 500):
                chunk = insert_payload[j:j + 500]
                values_sql = ",".join(
                    f"(:id_{k}, :user_id_{k}, :dedup_hash_{k}, :first_result_id_{k}, "
                    f":first_job_id_{k}, :parcel_id_{k}, :property_address_{k}, NOW())"
                    for k in range(len(chunk))
                )
                params = {}
                for k, c in enumerate(chunk):
                    params[f"id_{k}"] = c["id"]
                    params[f"user_id_{k}"] = c["user_id"]
                    params[f"dedup_hash_{k}"] = c["dedup_hash"]
                    params[f"first_result_id_{k}"] = c["first_result_id"]
                    params[f"first_job_id_{k}"] = c["first_job_id"]
                    params[f"parcel_id_{k}"] = c["parcel_id"]
                    params[f"property_address_{k}"] = c["property_address"]

                result = db.execute(
                    sa_text(f"""
                        INSERT INTO delivered_records
                            (id, user_id, dedup_hash, first_result_id,
                             first_job_id, parcel_id, property_address,
                             first_delivered_at)
                        VALUES {values_sql}
                        ON CONFLICT (user_id, dedup_hash) DO NOTHING
                        RETURNING dedup_hash
                    """),
                    params,
                )
                for row in result.fetchall():
                    claimed_hashes.add(row.dedup_hash)
            _logger.info("Job %s: dedup step 2 INSERT done — committing", job_id)
            db.commit()
            _logger.info("Job %s: dedup step 2 committed — %d claimed", job_id, len(claimed_hashes))

            # Step 3: any fresh Result whose dedup_hash is NOT in claimed_hashes
            # was a duplicate. Mark those rows.
            duplicate_result_ids = [
                str(row.id) for row in fresh_rows
                if row.dedup_hash not in claimed_hashes
            ]
            unique_count = len(claimed_hashes)
            dup_count = len(duplicate_result_ids)

            if duplicate_result_ids:
                # Batch the UPDATE to avoid an IN clause explosion.
                # Cast text[] to uuid[] — results.id is UUID type but
                # duplicate_result_ids are Python strings. Without the
                # cast, Postgres raises "operator does not exist: uuid = text".
                for j in range(0, len(duplicate_result_ids), 500):
                    chunk = duplicate_result_ids[j:j + 500]
                    db.execute(
                        sa_text(
                            "UPDATE results SET is_duplicate = true "
                            "WHERE id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": chunk},
                    )
                db.commit()

        _publish_log(
            r, job_id, "success",
            f"{len(records)} records saved ({unique_count} new leads, {dup_count} duplicates)",
            db=db,
        )

        # ── EXPORT ────────────────────────────────────────────────────────────
        from src.api.schemas import DeliverConfigDict
        deliver_config: DeliverConfigDict = cast(DeliverConfigDict, config.deliver or {})
        # Honor the user's chosen export format. DeliverConfig stores
        # `formats: list[str]`. We export the first format in the list;
        # if a user selected multiple, only the first is generated for
        # now (multi-format export is a separate feature). An empty or
        # missing list falls back to CSV. Previously the worker read a
        # `format` (singular) key that schemas.DeliverConfig never sets,
        # so every export silently came out as CSV regardless of the
        # user's selection — flagged by Codex adversarial review.
        formats = deliver_config.get("formats") or ["csv"]
        fmt = formats[0]

        _publish_log(r, job_id, "info", f"Building {fmt.upper()} export...", db=db)

        record_dicts = [r_obj.to_dict() for r_obj in records]
        exporter = DataExporter()
        local_file = exporter.export(record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt)

        object_key = f"exports/{job.user_id}/{job_id}/leads.{local_file.suffix.lstrip('.')}"
        try:
            exporter.upload_to_r2(local_file, object_key)
            _publish_log(r, job_id, "success", "Export uploaded to cloud storage", db=db)
        except Exception as upload_exc:
            _logger.warning("R2 upload failed (non-fatal): %s", upload_exc)
            _publish_log(r, job_id, "warning", "Cloud upload unavailable — export saved locally", db=db)
            object_key = None  # No cloud export available
        finally:
            local_file.unlink(missing_ok=True)

        # Atomic update of monthly record usage.
        # Sprint 6.4: duplicates delivered to this user in a prior scrape
        # do NOT count against the monthly quota. Records without a
        # dedup_hash (no parcel AND no address) still count, because
        # they are genuinely new data even though we can't dedupe them.
        billable_count = max(0, len(records) - dup_count)
        from sqlalchemy import update as sa_update
        db.execute(
            sa_update(User)
            .where(User.id == user.id)
            .values(records_used=User.records_used + billable_count)
        )
        db.commit()
        db.refresh(user)

        if user.records_limit != -1 and user.records_used > user.records_limit:
            overage = user.records_used - user.records_limit
            _publish_log(r, job_id, "warning", f"Plan limit exceeded by {overage} records. Upgrade to keep scraping.", db=db)

        # ── INLINE ENRICHMENT (BEFORE marking done) ──────────────────────────
        # Runs on this Celery task's thread, sharing the same DB session.
        # Earlier this code wrapped the call in a ThreadPoolExecutor with a
        # 5-minute future.result(timeout=...) so a hanging ArcGIS/assessor
        # HTTP request couldn't stall the worker forever. Two issues: (1)
        # the `with ThreadPoolExecutor(...) as executor:` exit waits for
        # the worker thread to finish, so the timeout never actually freed
        # the Celery worker on a real hang. (2) Passing the caller's
        # SQLAlchemy session across threads is unsafe and can corrupt
        # transaction state under concurrency. Both flagged by Codex
        # adversarial review. The Celery task's time_limit=3900s remains
        # the real hard cap; per-HTTP-call timeouts inside the enrichment
        # helpers (county_gis / king_county_assessor / etc) bound each
        # request's wait. If a hang slips through both, Celery hard-kills
        # the worker — which is what the previous thread guard was
        # actually relying on anyway.
        _publish_log(r, job_id, "info", "Looking up property and mailing addresses...", db=db)
        try:
            _run_inline_enrichment(db, job, r, job_id, config)
            _publish_log(r, job_id, "success", "Enrichment complete — addresses added", db=db)
        except Exception as exc:
            _logger.warning("Inline enrichment error: %s", str(exc)[:200])
            _publish_log(
                r, job_id, "warning",
                "Address enrichment failed — leads delivered without enriched fields",
                db=db,
            )

        # Re-export CSV with enriched data
        enriched_file = None
        try:
            refreshed = db.execute(
                select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
            ).scalars().all()
            record_dicts = [
                {c: getattr(res, c) for c in [
                    "date_recorded", "party_name", "heirs", "parcel_id",
                    "property_address", "mailing_address", "legal_description",
                    # Sprint 4: skip trace fields (may be null on first export
                    # if dispatcher hasn't submitted or webhook hasn't fired)
                    "phone", "phone_type", "email", "skip_trace_status",
                ]}
                for res in refreshed
            ]
            enriched_file = exporter.export(record_dicts, filename=f"job_{job_id[:8]}", fmt=fmt)
            if object_key:
                exporter.upload_to_r2(enriched_file, object_key)
                _logger.info("Re-exported CSV with enriched data")
        except Exception as exc:
            _logger.warning("CSV re-export failed: %s", str(exc)[:60])
        finally:
            if enriched_file:
                enriched_file.unlink(missing_ok=True)

        # ── NOW mark done (after enrichment + re-export) ────────────────────
        # record_count reflects unique (non-duplicate) leads — what the user
        # actually sees on the results page. The raw scrape total is in the
        # log: "{N} records saved ({unique} new leads, {dup} duplicates)".
        display_count = max(0, len(records) - dup_count)
        _set_status(
            db, job, "done",
            finished_at=_now(),
            record_count=display_count,
            export_key=object_key,
        )
        _publish_log(r, job_id, "success", f"Job complete — {display_count} new leads ({dup_count} duplicates filtered)", db=db)
        r.publish(f"job_logs:{job_id}", json.dumps({"type": "done", "record_count": display_count}))

        # ── EMAIL DELIVERY ─────────────────────────────────────────────────────
        emails = deliver_config.get("emails", [])
        if emails and object_key:
            try:
                download_url = _delivery_download_url(job_id, job.user_id, object_key, exporter)
                deliver_job_results(
                    job_id=job_id,
                    scraper_name=config.name,
                    record_count=display_count,
                    download_url=download_url,
                    recipient_emails=emails,
                    fmt=fmt,
                )
            except Exception as email_exc:
                _logger.warning("Email delivery failed (non-fatal): %s", email_exc)
                _publish_log(r, job_id, "warning", "Email delivery unavailable", db=db)

        # ── SPRINT 6.5: WEBHOOK DELIVERY ───────────────────────────────────────
        # Business+ plan feature (gated at scraper config creation time).
        # Fire-and-forget via Celery so retries happen on the celery queue
        # independently of the scrape job. Non-fatal: webhook failures
        # must never mark the scrape job as errored.
        webhook_url = deliver_config.get("webhook_url")
        if webhook_url and object_key:
            try:
                from src.workers.webhook_delivery import (
                    build_webhook_payload,
                    deliver_job_webhook,
                )
                signed_download = _delivery_download_url(job_id, job.user_id, object_key, exporter)
                webhook_secret = deliver_config.get("webhook_secret")
                payload = build_webhook_payload(
                    job_id=job_id,
                    scraper_config_id=str(config.id),
                    scraper_name=config.name,
                    county=config.county,
                    state=config.state,
                    record_type=config.record_type,
                    status="done",
                    record_count=len(records),
                    started_at=job.started_at,
                    finished_at=_now(),
                    export_key=object_key,
                    fmt=fmt,
                    download_url=signed_download,
                    webhook_secret=webhook_secret,
                )
                deliver_job_webhook.delay(job_id, webhook_url, payload)
                _publish_log(
                    r, job_id, "info",
                    f"Webhook queued for delivery to {webhook_url[:60]}",
                    db=db,
                )
            except Exception as webhook_exc:
                _logger.warning(
                    "Webhook enqueue failed (non-fatal) for job %s: %s",
                    job_id, str(webhook_exc)[:200],
                )
                _publish_log(
                    r, job_id, "warning",
                    "Webhook queue unavailable — job completed successfully",
                    db=db,
                )


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _run_scraper(
    scraper_class,
    date_from: str,
    date_to: str,
    r: sync_redis.Redis,
    job_id: str,
    on_progress: "ProgressCallback | None" = None,
    record_type: str | None = None,
):
    """Run the async scraper and stream progress logs back to Redis."""
    # Pass record_type if the scraper accepts it — template/AI scrapers may not
    import inspect
    kwargs = {}
    if record_type:
        try:
            sig = inspect.signature(scraper_class)
            if "record_type" in sig.parameters:
                kwargs["record_type"] = record_type
        except (ValueError, TypeError):
            pass
    async with scraper_class(**kwargs) as scraper:
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
    from sqlalchemy import func, select as sa_select
    from src.db.models import Result

    all_results = db.execute(
        sa_select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
    ).scalars().all()

    # GIS batch enrichment for property AND mailing addresses
    # Run for records missing either property address or mailing address
    results_need_addr = [
        res for res in all_results
        if res.parcel_id and len(res.parcel_id.strip()) >= 6
        and (not res.property_address or res.property_address == "(enrichment unavailable)"
             or not res.mailing_address)
    ]
    if results_need_addr:
        _publish_log(r, job_id, "info", f"Looking up {len(results_need_addr)} property addresses...", db=db)
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

    # Name-based PACS fallback for records with no parcel (e.g. probate
    # estate filings: Cert of Death, Letters Testamentary, Personal Rep
    # Deed). These carry owner/heir names but no parcel_id in the
    # recording index. The county connector's assessor_url points at the
    # Tyler PACS PropertyAccess portal; we search it by owner name to
    # hydrate property_address. Skip trace downstream requires an
    # address, so this is what unlocks phone/email lookup for probate.
    from src.db.models import CountyConnector
    _conn_row = db.execute(
        sa_select(CountyConnector).where(
            func.lower(CountyConnector.county) == config.county.lower(),
            func.upper(CountyConnector.state) == config.state.upper(),
            CountyConnector.active,
        )
    ).scalars().first()
    connector_assessor_url = getattr(_conn_row, "assessor_url", None) if _conn_row else None
    # Fall back to the hardcoded _KNOWN_ASSESSOR_URLS map so PACS enrichment
    # works even when the connector row's assessor_url is still NULL
    # (e.g. migration 022 not yet applied to this environment).
    if not connector_assessor_url:
        from src.scrapers.enrichment.ai_assessor import _KNOWN_ASSESSOR_URLS
        key = f"{config.county.lower()}_{config.state.upper()}"
        connector_assessor_url = _KNOWN_ASSESSOR_URLS.get(key)
    from src.scrapers.enrichment.pacs import is_pacs_url, batch_lookup_pacs_by_name
    if connector_assessor_url and is_pacs_url(connector_assessor_url):
        results_no_addr = [
            res for res in all_results
            if res.party_name
            and not res.property_address
        ]
        if results_no_addr:
            _publish_log(
                r, job_id, "info",
                f"Looking up {len(results_no_addr)} addresses via PACS by owner name...",
                db=db,
            )
            names = [res.party_name for res in results_no_addr]
            pacs_results = batch_lookup_pacs_by_name(
                connector_assessor_url, names, max_workers=5,
            )
            name_hits = 0
            for res, pacs in zip(results_no_addr, pacs_results):
                if not pacs:
                    continue
                if pacs.get("address"):
                    res.property_address = pacs["address"]
                if pacs.get("mailing") and not res.mailing_address:
                    res.mailing_address = pacs["mailing"]
                if pacs.get("parcel_id") and not res.parcel_id:
                    res.parcel_id = pacs["parcel_id"]
                name_hits += 1
            if name_hits:
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    db.commit()
            _publish_log(
                r, job_id, "info",
                f"Found {name_hits}/{len(results_no_addr)} addresses via PACS",
                db=db,
            )

    # King County: eRealProperty + Tax Bill for property + mailing
    if config.county.lower() == "king" and config.state.upper() == "WA":
        needs = [
            res for res in all_results
            if res.parcel_id and len(res.parcel_id.strip()) >= 6
            and not res.mailing_address
        ]
        if needs:
            _publish_log(r, job_id, "info", f"Looking up {len(needs)} mailing addresses...", db=db)
            from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county
            pids = list({res.parcel_id.strip() for res in needs})
            pid_map: dict[str, list] = {}
            for res in needs:
                pid = res.parcel_id.strip()
                if pid not in pid_map:
                    pid_map[pid] = []
                pid_map[pid].append(res)
            # Cap at 300 parcels to avoid 10+ minute hangs on large batches.
            # King County assessor is slow (~0.5s per parcel) — 1354 parcels
            # takes ~11 min which exceeds the enrichment timeout.
            _MAX_KING_PARCELS = 300
            if len(pids) > _MAX_KING_PARCELS:
                _logger.info("Capping King County mailing lookup to %d/%d parcels", _MAX_KING_PARCELS, len(pids))
                pids = pids[:_MAX_KING_PARCELS]
            enriched = asyncio.run(asyncio.wait_for(batch_enrich_king_county(pids), timeout=240))
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
            _publish_log(r, job_id, "info", f"Found {found}/{len(pids)} mailing addresses", db=db)

    # ── Post-enrichment: log unactionable records (kept for visibility) ──
    # Records with no property_address and no mailing_address can't be
    # mailed, but we keep them in the DB so users see what was scraped.
    # The frontend shows them with empty address fields ("—").
    fresh = db.execute(
        sa_select(Result).where(Result.job_id == job_id, Result.user_id == job.user_id)
    ).scalars().all()
    unactionable = [
        res for res in fresh
        if not (res.property_address and res.property_address != "(enrichment unavailable)")
        and not res.mailing_address
    ]
    if unactionable:
        _publish_log(
            r, job_id, "info",
            f"{len(unactionable)} records have no deliverable address "
            f"(upstream data gap — parcel had no GIS address)",
            db=db,
        )
        _logger.info(
            "Job %s: %d/%d records have no deliverable address (kept for visibility)",
            job_id, len(unactionable), len(fresh),
        )

    # ── Sprint 4: skip trace enqueue ─────────────────────────────────────
    # Only runs if:
    #   1. SKIP_TRACE_ENABLED globally (env flag)
    #   2. The user's scraper config has skip_trace_enabled=True
    #   3. The user's plan permits skip trace (Starter blocked)
    #   4. TRACERFY_API_TOKEN is configured
    # Matching records are either hydrated from skip_trace_cache (free)
    # or inserted into pending_skip_trace_rows for the dispatcher to
    # submit in a batch. Actual Tracerfy calls happen in the dispatcher;
    # this step is instant and never blocks scrape completion.
    _enqueue_skip_trace_rows(db, job, r, job_id, config)


def _enqueue_skip_trace_rows(db, job, r, job_id: str, config) -> None:
    """Enqueue eligible Result rows into pending_skip_trace_rows.

    Runs at the end of _run_inline_enrichment, after GIS + county
    assessor + post-enrichment cleanup. Only records that survived the
    cleanup (have a property_address) are eligible.
    """
    # Local imports — sa_select must be imported here because the module-
    # level import is scoped inside _run_inline_enrichment, not globally
    from sqlalchemy import select as sa_select
    from src.db.models import PendingSkipTraceRow, Result, SkipTraceCache
    from src.scrapers.enrichment.skip_trace import (
        address_cache_key,
        build_pending_row_payload,
    )

    if not settings.SKIP_TRACE_ENABLED:
        return
    if not settings.TRACERFY_API_TOKEN:
        _publish_log(
            r, job_id, "warning",
            "Skip trace requested but TRACERFY_API_TOKEN not configured",
            db=db,
        )
        return
    if not getattr(config, "skip_trace_enabled", False):
        return
    # Plan gate: Starter excluded. Pro/Business/Agency allowed.
    if (job.user.plan or "starter").lower() == "starter":
        _publish_log(
            r, job_id, "warning",
            "Skip trace requested but user plan (starter) does not include it. "
            "Upgrade to Pro to unlock skip trace ($0.08/lookup).",
            db=db,
        )
        return

    # Reload the surviving results after the unactionable drop
    eligible = db.execute(
        sa_select(Result).where(
            Result.job_id == job_id,
            Result.user_id == job.user_id,
            Result.property_address.isnot(None),
            Result.skip_trace_status == "not_attempted",
        )
    ).scalars().all()

    if not eligible:
        return

    cache_hits = 0
    cache_misses = 0
    enqueued_normal = 0
    enqueued_advanced = 0

    for rec in eligible:
        # Parse the combined address to get canonical city/state for the cache key
        payload = build_pending_row_payload(rec)
        if payload is None:
            continue

        cache_key = address_cache_key(
            payload["property_address"],
            payload["city"],
            payload["state"],
        )
        cached = db.get(SkipTraceCache, cache_key)
        cache_valid = False
        if cached:
            # 90-day TTL check
            age = _now() - cached.fetched_at
            if age.days < settings.SKIP_TRACE_CACHE_DAYS:
                cache_valid = True

        if cache_valid:
            # Copy cached values directly to the Result — no Tracerfy call
            rec.phone = cached.phone
            rec.phone_type = cached.phone_type
            rec.phone_dnc_flag = cached.phone_dnc_flag
            rec.email = cached.email
            rec.skip_trace_status = "hit" if (cached.phone or cached.email) else "miss"
            rec.skip_trace_attempted_at = _now()
            cache_hits += 1
        else:
            # Enqueue for the dispatcher. Truncate string fields to fit
            # VARCHAR(128) — code violation descriptions can be 250+ chars
            # and crash the INSERT with StringDataRightTruncation, which
            # poisons the session with PendingRollbackError and hangs the job.
            def _trunc128(v: str | None) -> str | None:
                return v[:128] if v and len(v) > 128 else v

            try:
                pending = PendingSkipTraceRow(
                    job_id=payload["job_id"],
                    result_id=payload["result_id"],
                    user_id=payload["user_id"],
                    property_address=_trunc128(payload["property_address"]),
                    city=_trunc128(payload["city"]),
                    state=_trunc128(payload["state"]),
                    zip=_trunc128(payload["zip"]),
                    first_name=_trunc128(payload["first_name"]),
                    last_name=_trunc128(payload["last_name"]),
                    mail_address=_trunc128(payload["mail_address"]),
                    mail_city=_trunc128(payload["mail_city"]),
                    mail_state=_trunc128(payload["mail_state"]),
                    mail_zip=_trunc128(payload["mail_zip"]),
                    trace_type=payload["trace_type"],
                    status="queued",
                )
                db.add(pending)
            except Exception as exc:
                _logger.warning("Skip trace enqueue failed for %s: %s", rec.party_name[:30] if rec.party_name else "?", str(exc)[:80])
                db.rollback()
                continue
            rec.skip_trace_status = "queued"
            cache_misses += 1
            if payload["trace_type"] == "advanced":
                enqueued_advanced += 1
            else:
                enqueued_normal += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        db.commit()

    _publish_log(
        r, job_id, "info",
        f"Skip trace: {cache_hits} cache hits, {cache_misses} queued "
        f"({enqueued_normal} normal + {enqueued_advanced} advanced). "
        f"Dispatcher submits batches every 5 min.",
        db=db,
    )
    _logger.info(
        "Job %s skip trace enqueue: cache_hits=%d queued=%d (normal=%d advanced=%d)",
        job_id, cache_hits, cache_misses, enqueued_normal, enqueued_advanced,
    )


def _fail_job(db, job, r, job_id: str, reason: str) -> None:
    """Transition job to FAILED with a human-readable error message.

    H3 (full-SaaS review): the previous implementation rolled back
    the main session before calling _set_status and _publish_log.
    If any JobLog rows had been queued via _publish_log(db=db) but
    not yet committed in the same transaction, the rollback
    destroyed them — losing the failure context for the user. The
    final failure log line also ran against a session that had
    just been rolled back, which is a fragile code path.

    Now:
      1. The state transition (jobs.status = 'failed') goes through
         the main session so _set_status can commit + refresh
         normally. If the main session was in a failed transaction
         state from an upstream exception, we recover it once with
         rollback() before the update.
      2. The failure-log _publish_log call passes db=None so it
         opens a fresh system_sync_session for the INSERT. This
         guarantees the failure message lands in job_logs even if
         the main session is misbehaving.
    """
    try:
        db.rollback()  # Recover from any pending failed transaction
    except Exception:
        pass
    try:
        _set_status(db, job, "failed", finished_at=_now(), error_message=reason)
    except Exception as exc:
        _logger.error(
            "Job %s: _set_status failed during _fail_job: %s",
            job_id, str(exc)[:200],
        )
    # Publish the failure log via a fresh session (db=None) so it
    # is not coupled to the main session's transaction state.
    _publish_log(r, job_id, "error", reason, db=None)
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


def _resolve_date_range(schedule: dict, config_id: str | None = None, job_id: str | None = None, user_plan: str = "starter") -> tuple[str, str]:
    """Compute date_from and date_to from a scraper's schedule config."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    # Use US/Pacific date since all counties are in WA.
    # Worker runs in UTC where midnight is already "tomorrow" for Pacific users,
    # causing the rolling range to be off by +1 day.
    today = datetime.now(ZoneInfo("US/Pacific")).date()

    # Starter (free) tier gets a 7-day data delay — daily freshness
    # is the paid moat. Starter users see records from 7+ days ago.
    _STARTER_DELAY_DAYS = 7
    if user_plan == "starter":
        end_date = today - timedelta(days=_STARTER_DELAY_DAYS)
        _logger.info("Starter plan: applying %d-day data delay (end_date=%s)", _STARTER_DELAY_DAYS, end_date)
    else:
        end_date = today
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
        # Look up the last completed job for this scraper config and use
        # its date range end as our start. If no previous job exists,
        # fall back to 30 days (not 90 — avoids massive duplicate sets).
        from src.db.models import Job
        from src.db.session import SyncSessionLocal
        try:
            with SyncSessionLocal() as _db:
                last_job = _db.execute(
                    select(Job).where(
                        Job.scraper_config_id == config_id,
                        Job.status == "done",
                        Job.id != job_id,  # exclude current job
                    ).order_by(Job.finished_at.desc()).limit(1)
                ).scalar()
                if last_job and last_job.finished_at:
                    # Start from the day AFTER the last job finished to
                    # avoid re-scraping records already in that job's
                    # results. Without +1 day, every record from the
                    # overlap day is a guaranteed duplicate.
                    date_from = last_job.finished_at.date() + timedelta(days=1)
                    _logger.info(
                        "since_last_run: last job %s finished %s, scraping from %s",
                        last_job.id, last_job.finished_at.date(), date_from,
                    )
                else:
                    date_from = today - timedelta(days=30)
                    _logger.info(
                        "since_last_run: no previous done job for config_id=%s, defaulting to 30 days",
                        config_id,
                    )
        except Exception as exc:
            _logger.error("since_last_run: DB lookup failed (%s), defaulting to 30 days", exc)
            date_from = today - timedelta(days=30)
    elif range_mode == "rolling_30":
        date_from = end_date - timedelta(days=30)
    elif range_mode == "rolling_7":
        date_from = end_date - timedelta(days=7)
    else:
        date_from = end_date - timedelta(days=90)

    return date_from.strftime("%m/%d/%Y"), end_date.strftime("%m/%d/%Y")
