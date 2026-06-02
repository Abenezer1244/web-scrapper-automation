"""Celery task: download and ingest a Tracerfy batch completion CSV.

Previously this lived in src/api/routes/webhooks.py as a FastAPI
BackgroundTask attached to the Tracerfy webhook handler. The
problem with BackgroundTasks (M8 from the full-SaaS review) is
that they run inside the same API process after the 200 response
is returned — if the API process restarts between 200-return and
CSV parse completion, the ingest is dropped on the floor. Tracerfy
does not retry, so the downstream effect is "batch of skip-trace
results quietly lost" with no way to recover beyond manually
re-enqueuing the PendingSkipTraceRow rows.

Moving this to a Celery task puts the ingest behind Redis-backed
durable queueing: if the worker dies mid-task, Celery retries
(task_acks_late is on for all our tasks). If Redis itself is
unavailable the webhook receiver will itself fail, but that's
visible at the HTTP layer and can be handled.

The task body is intentionally a near-verbatim move of the old
function. Future cleanups (batched updates, better error
reporting) should land separately so the M8 diff stays focused
on the queueing change.
"""

from datetime import UTC, datetime
from urllib.parse import urlsplit

from src.config import settings
from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("worker.tracerfy_ingest")


@app.task(
    bind=True,
    acks_late=True,
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def report_skip_trace_meter_event(self, **event) -> None:
    """REDTEAM (Codex convergence): durably report ONE Stripe skip-trace
    MeterEvent.

    Split out of ``ingest_tracerfy_batch`` so a Stripe/network failure RETRIES
    instead of being swallowed. Previously the meter call ran inline after the
    DB commit and only logged on failure — but the same committed transaction
    already advanced the local usage counter AND marked the SkipTraceQueue
    ``completed``, so the completed-status guard blocks any ingest replay. The
    net effect was: usage consumed locally, MeterEvent lost, account
    under-billed with no recovery. The MeterEvent carries a stable
    ``(queue_id, user_id)`` identifier, so a retry is idempotent and never
    double-bills. After ``max_retries`` the failure surfaces loudly for ops.
    """
    from src.api.billing.skip_trace_usage import report_meter_event_to_stripe
    report_meter_event_to_stripe(**event)


def _host_is_tracerfy(download_url: str) -> bool:
    """REDTEAM B1/T3: confirm a webhook-supplied download_url points at a
    Tracerfy-owned host before we fetch it server-side.

    The webhook body is shared-secret authenticated, but the secret is a
    static URL path component — a leaked/guessed secret lets an attacker
    POST an arbitrary download_url and have our worker fetch it (SSRF).
    download_tracerfy_csv() already routes through safe_get_following
    (blocks private/metadata IPs + validates every redirect hop), but it
    deliberately allows ANY public host. Tracerfy delivers CSVs from its
    own domain and its DigitalOcean Spaces CDN bucket, so we additionally
    pin the host to those before trusting the URL.

    Allowed:
      - the configured TRACERFY_API_BASE_URL host (and its subdomains)
      - DigitalOcean Spaces CDN buckets owned by Tracerfy
        (e.g. tracerfy.nyc3.cdn.digitaloceanspaces.com)
    """
    try:
        parts = urlsplit(download_url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False

    api_host = (urlsplit(settings.TRACERFY_API_BASE_URL).hostname or "").lower()
    if api_host and (host == api_host or host.endswith("." + api_host)):
        return True

    # DigitalOcean Spaces CDN: bucket name is the leftmost label and must
    # be Tracerfy's own bucket. Matches "<bucket>.<region>.cdn.digitalocean
    # spaces.com" and "<bucket>.<region>.digitaloceanspaces.com".
    if host.endswith(".digitaloceanspaces.com") and host.split(".", 1)[0] == "tracerfy":
        return True

    return False


@app.task(
    name="src.workers.tracerfy_ingest.ingest_tracerfy_batch",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    acks_late=True,
)
def ingest_tracerfy_batch(
    self,
    queue_id: int,
    download_url: str,
    rows_uploaded: int = 0,
    credits_deducted: int = 0,
) -> dict:
    """Download a Tracerfy batch CSV and upsert phone/email into Results.

    On failure, Celery auto-retries with exponential backoff. After
    max_retries attempts the task gives up and the batch is marked
    errored on the SkipTraceQueue row so ops can see what happened.
    """
    from sqlalchemy import select, update

    from src.db.models import (
        PendingSkipTraceRow,
        Result,
        SkipTraceCache,
        SkipTraceQueue,
    )
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.skip_trace import (
        TracerfyError,
        address_cache_key,
        download_tracerfy_csv,
        ingest_webhook_csv,
    )

    # REDTEAM T3: refuse to fetch a forged/body-supplied download_url that
    # does not point at a Tracerfy-owned host. Reject BEFORE any DB work or
    # network fetch so a forged webhook is a cheap, logged no-op.
    if not _host_is_tracerfy(download_url):
        _logger.error(
            "Refusing Tracerfy ingest for queue %d: download_url host not "
            "Tracerfy-owned (host=%s)",
            queue_id,
            (urlsplit(download_url).hostname or "<none>"),
        )
        return {"queue_id": queue_id, "skipped": "untrusted_download_host"}

    try:
        csv_text = download_tracerfy_csv(download_url)
    except TracerfyError as exc:
        _logger.error("Failed to download CSV for queue %d: %s", queue_id, exc)
        raise  # autoretry will catch and retry

    try:
        parsed = ingest_webhook_csv(csv_text)
    except Exception as exc:
        _logger.error(
            "Failed to parse CSV for queue %d: %s", queue_id, str(exc)[:200]
        )
        # Parse errors are unlikely to be transient — don't retry
        raise self.retry(exc=exc, max_retries=0)

    hit_count = 0
    miss_count = 0
    now = datetime.now(UTC)

    # SYSTEM SESSION: a single Tracerfy batch can contain pending
    # rows from multiple users (the dispatcher groups by
    # trace_type, not user). This ingest path legitimately needs
    # to update Result rows across tenants.
    with system_sync_session() as db:
        # REDTEAM B1: replay/idempotency guard. Lock the SkipTraceQueue row
        # FOR UPDATE as the FIRST statement so the lock is held through the
        # ingest + billing + status flip below, all inside this single
        # transaction (committed once at db.commit()). Replaying a
        # completed-batch webhook previously re-ran ingest and re-incremented
        # skip_trace_used_this_month, double-billing the victim. Now a
        # concurrent replay blocks on this lock, then sees status="completed"
        # and no-ops; a serial replay sees it immediately. Missing row =
        # unknown/forged queue id → refuse.
        queue_row = (
            db.execute(
                select(SkipTraceQueue)
                .where(SkipTraceQueue.tracerfy_queue_id == queue_id)
                .with_for_update()
            )
            .scalars()
            .first()
        )
        if queue_row is None:
            _logger.warning(
                "Tracerfy ingest queue %d: no SkipTraceQueue row — refusing "
                "to process an unknown/forged batch id",
                queue_id,
            )
            return {"queue_id": queue_id, "skipped": "unknown_queue"}
        if queue_row.status in ("completed", "billed", "errored"):
            _logger.info(
                "Tracerfy ingest queue %d: already %s — idempotent no-op",
                queue_id,
                queue_row.status,
            )
            return {"queue_id": queue_id, "skipped": f"already_{queue_row.status}"}

        pending = (
            db.execute(
                select(PendingSkipTraceRow)
                .where(PendingSkipTraceRow.tracerfy_queue_id == queue_id)
            )
            .scalars()
            .all()
        )

        pending_by_key: dict[tuple[str, str, str], list] = {}
        for p in pending:
            key = (
                (p.property_address or "").strip().lower(),
                (p.city or "").strip().lower(),
                (p.state or "").strip().upper(),
            )
            pending_by_key.setdefault(key, []).append(p)

        for csv_row in parsed:
            csv_key = (
                (csv_row.get("address") or "").strip().lower(),
                (csv_row.get("city") or "").strip().lower(),
                (csv_row.get("state") or "").strip().upper(),
            )
            matches = pending_by_key.get(csv_key, [])
            if not matches:
                continue

            phone = csv_row.get("phone")
            email = csv_row.get("email")
            is_hit = bool(phone or email)
            if is_hit:
                hit_count += 1
            else:
                miss_count += 1

            # Cache the result for 90-day reuse (Sprint 4)
            cache_key = address_cache_key(
                csv_row.get("address") or "",
                csv_row.get("city") or "",
                csv_row.get("state") or "",
            )
            existing_cache = db.get(SkipTraceCache, cache_key)
            if existing_cache:
                existing_cache.phone = phone
                existing_cache.phone_type = csv_row.get("phone_type")
                existing_cache.email = email
                existing_cache.fetched_at = now
            else:
                db.add(
                    SkipTraceCache(
                        address_hash=cache_key,
                        phone=phone,
                        phone_type=csv_row.get("phone_type"),
                        phone_dnc_flag=None,
                        email=email,
                        raw_response=csv_row.get("raw"),
                        fetched_at=now,
                    )
                )

            # Update matched Result rows — user_id filter for H10
            for p in matches:
                db.execute(
                    update(Result)
                    .where(
                        Result.id == p.result_id,
                        Result.user_id == p.user_id,
                    )
                    .values(
                        phone=phone,
                        phone_type=csv_row.get("phone_type"),
                        email=email,
                        skip_trace_status="hit" if is_hit else "miss",
                        skip_trace_attempted_at=now,
                    )
                )
                db.execute(
                    update(PendingSkipTraceRow)
                    .where(PendingSkipTraceRow.id == p.id)
                    .values(status="completed")
                )

        # Mark the Tracerfy queue record as completed
        db.execute(
            update(SkipTraceQueue)
            .where(SkipTraceQueue.tracerfy_queue_id == queue_id)
            .values(
                status="completed",
                download_url=download_url,
                completed_at=now,
                rows_uploaded=rows_uploaded,
                credits_deducted=credits_deducted,
            )
        )

        # REDTEAM B1+B2: advance each user's skip-trace counter HERE, BEFORE
        # the single db.commit() below, so the counter advance, the pending-
        # row "completed" flips, and the SkipTraceQueue status="completed"
        # flip all land in ONE transaction under the queue-row lock taken at
        # the top of this block. This is the idempotency anchor: a replay of
        # this batch finds status="completed" and no-ops before reaching this
        # point, so the counter can never be re-advanced. report_usage_from_
        # webhook no longer commits or calls Stripe — it returns the deferred
        # Stripe MeterEvents for us to fire AFTER commit.
        pending_meter_events: list[dict] = []
        try:
            from src.api.billing.skip_trace_usage import (
                report_usage_from_webhook,
            )
            billing_summary = report_usage_from_webhook(db, queue_id)
            pending_meter_events = billing_summary.get("pending_meter_events", [])
            _logger.info(
                "Tracerfy ingest queue=%d: %d hits / %d misses / billing=%s",
                queue_id, hit_count, miss_count, billing_summary,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            # A billing-advance failure must roll back the whole ingest so we
            # never mark the queue completed while leaving counters un-advanced
            # (which a replay could then never correct). Re-raise to abort the
            # transaction; Celery will retry, and the retry will re-process
            # because the queue was never committed as completed.
            _logger.error(
                "Skip-trace counter advance failed for queue %d: %s",
                queue_id, str(exc)[:200],
            )
            raise

        # Single atomic commit: ingest + status flip + counter advances.
        db.commit()

    # REDTEAM B2 phase 2 (+ Codex convergence): fire the Stripe MeterEvents
    # AFTER commit / lock release, but ENQUEUE them as their own retrying Celery
    # task rather than calling inline-and-swallowing. The counter is already
    # durably advanced and the queue is marked completed (so an ingest replay
    # no-ops) — therefore the meter event MUST be reported durably or the
    # account is silently under-billed. report_skip_trace_meter_event retries
    # on transient Stripe failures and dedupes via its stable (queue_id,
    # user_id) identifier, so it can neither be lost nor double-billed.
    for ev in pending_meter_events:
        try:
            report_skip_trace_meter_event.delay(**ev)
        except Exception as exc:  # noqa: BLE001 — enqueue failure must not fail ingest
            _logger.error(
                "Failed to enqueue Stripe meter report for queue %d user %s: %s "
                "— usage advanced but meter event not queued",
                queue_id, str(ev.get("user_id", "?"))[:8], str(exc)[:200],
            )

    return {
        "queue_id": queue_id,
        "hits": hit_count,
        "misses": miss_count,
    }
