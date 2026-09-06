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
def report_skip_trace_meter_event(self, outbox_id: str) -> dict:
    """REDTEAM (Codex convergence — meter outbox): durably report ONE Stripe
    skip-trace MeterEvent from its outbox row.

    Driven entirely off the persisted ``skip_trace_meter_events`` outbox row
    (by id) rather than in-memory kwargs, so an event survives broker-down /
    worker-crash: report_usage_from_webhook commits the row in the same
    transaction that advances the usage counter, and either this inline
    enqueue or the ``flush_skip_trace_meter_outbox`` beat sweep delivers it.

    Idempotency:
      * if ``reported_at`` is already set, this is a no-op (a retry, a
        duplicate enqueue, or a beat sweep racing the inline enqueue);
      * the Stripe MeterEvent uses the STABLE ``skip_trace_q{queue}_u{user}``
        identifier, so even a double-fire dedupes server-side.

    Durability: a transient Stripe/network failure RAISES (it is no longer
    swallowed), so ``autoretry_for=(Exception,)`` retries with backoff. A
    terminal condition (Stripe not configured, no customer id, nothing to
    bill) stamps ``reported_at`` and stops — no pointless retries. After
    ``max_retries`` a real failure surfaces loudly for ops.
    """
    from datetime import UTC, datetime

    from src.api.billing.skip_trace_usage import (
        _StripeNotConfiguredError,
        report_meter_event_to_stripe,
    )
    from src.db.models import SkipTraceMeterEvent
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        row = db.get(SkipTraceMeterEvent, outbox_id)
        if row is None:
            _logger.warning(
                "Skip-trace meter outbox %s: row not found — nothing to report",
                outbox_id,
            )
            return {"outbox_id": outbox_id, "skipped": "not_found"}
        if row.reported_at is not None:
            # Already reported by a prior attempt / the inline enqueue / a
            # beat sweep. Idempotent no-op.
            return {"outbox_id": outbox_id, "skipped": "already_reported"}

        # Terminal no-ops (_StripeNotConfiguredError) still stamp reported_at so the
        # row stops being swept; transient Stripe failures propagate to
        # autoretry. The stable identifier inside makes any retry idempotent.
        try:
            report_meter_event_to_stripe(
                user_id=str(row.user_id),
                queue_id=row.tracerfy_queue_id,
                billable_units=row.billable_units,
                stripe_customer_id=row.stripe_customer_id,
                plan=row.plan or "",
            )
        except _StripeNotConfiguredError as exc:
            _logger.warning(
                "Skip-trace meter outbox %s: terminal no-op (%s) — marking "
                "reported to stop the sweep",
                outbox_id, exc,
            )

        row.reported_at = datetime.now(UTC)
        db.commit()

    return {"outbox_id": outbox_id, "reported": True}


def _alert_unreconciled(
    queue_id: int, n_unmatched: int, n_unmatched_csv: int, n_pending: int
) -> None:
    """Page ops when a completed batch left rows we could not match.

    The address key is `(property_address, city, state)` compared verbatim
    between what we sent and what Tracerfy echoed back. A systematic mismatch
    (provider-side USPS standardisation, say) would show up here as a whole
    batch failing at once rather than as leads quietly stuck on "Processing".
    """
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "skip_trace", f"unreconciled_{queue_id}",
            "Skip-trace results could not be matched back to leads",
            f"Tracerfy queue {queue_id} completed, but {n_unmatched} of "
            f"{n_pending} submitted row(s) never appeared in the result CSV "
            f"({n_unmatched_csv} CSV row(s) also matched nothing on our side). "
            f"Those leads are now marked 'errored' instead of sitting on "
            f"'Processing' forever, and they were NOT billed to the user.\n\n"
            f"Rows are matched on (property_address, city, state) compared "
            f"verbatim against Tracerfy's echoed address. If this fires for a "
            f"whole batch the provider is likely normalising addresses and the "
            f"match key needs revisiting; if it fires for one or two rows they "
            f"were probably rejected at upload for a malformed address.",
        )
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        _logger.warning("unreconciled-batch ops alert failed: %s", str(exc)[:120])


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

    # REDTEAM (Codex review): cheap pre-check BEFORE any network I/O. A replay
    # of an already completed/billed/errored batch — or an unknown/forged queue
    # id that happened to pass the host check — must no-op WITHOUT downloading
    # the (possibly expired/slow) signed CSV URL. The authoritative guard is
    # still the SELECT ... FOR UPDATE re-check below (handles the race where the
    # status flips between this peek and the lock); this only spares the
    # wasteful/erroring fetch for the common replay/forged case.
    with system_sync_session() as _precheck:
        _pre = _precheck.execute(
            select(SkipTraceQueue.status).where(
                SkipTraceQueue.tracerfy_queue_id == queue_id
            )
        ).first()
    if _pre is None:
        _logger.warning(
            "Tracerfy ingest queue %d: unknown/forged batch id — no-op (pre-download)",
            queue_id,
        )
        return {"queue_id": queue_id, "skipped": "unknown_queue"}
    if _pre[0] in ("completed", "billed", "errored"):
        _logger.info(
            "Tracerfy ingest queue %d: already %s — idempotent no-op (pre-download)",
            queue_id, _pre[0],
        )
        return {"queue_id": queue_id, "skipped": f"already_{_pre[0]}"}

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

        # Reconciliation bookkeeping. Both directions of a mismatch used to be
        # invisible: a CSV row matching nothing was silently `continue`d, and a
        # pending row that no CSV row ever named simply stayed 'submitted' — so
        # its lead read "Processing" forever and it was never billed (the usage
        # rollup counts only status='completed'). Neither left a counter, a log
        # line or a terminal state. Count both and settle them below.
        unmatched_csv = 0
        matched_pending_ids: set = set()

        def _key_of(row: dict) -> tuple[str, str, str]:
            return (
                (row.get("address") or "").strip().lower(),
                (row.get("city") or "").strip().lower(),
                (row.get("state") or "").strip().upper(),
            )

        # Rows are attributed on (address, city, state) compared against
        # Tracerfy's echoed address. That is safe while a key names ONE property
        # — several of our rows can share it (same property, same owner) and all
        # correctly receive the same contacts. It stops being safe when the same
        # key carries several DIFFERENT owners and Tracerfy returns a CSV row per
        # owner: every CSV row then matches every pending row and the last one
        # silently wins, stamping one person's phone and email onto another
        # person's lead (Codex).
        #
        # Measured before deciding what to do about it: across every row ever
        # submitted, production has exactly one in-batch key collision, and it is
        # the benign shape (same owner, same tenant, two results at one address).
        # Zero collisions carry different names; zero span tenants. So the key is
        # left alone — narrowing it to include the name would break ADVANCED
        # traces, which deliberately send no name and get back whichever owner
        # Tracerfy identifies — and the dangerous shape is refused instead.
        csv_key_counts: dict[tuple[str, str, str], int] = {}
        for _row in parsed:
            _k = _key_of(_row)
            csv_key_counts[_k] = csv_key_counts.get(_k, 0) + 1

        for csv_row in parsed:
            csv_key = _key_of(csv_row)
            matches = pending_by_key.get(csv_key, [])
            if not matches:
                unmatched_csv += 1
                continue
            if csv_key_counts[csv_key] > 1 and len(
                {(p.first_name or "", p.last_name or "") for p in matches}
            ) > 1:
                # Several results for this address AND several distinct owners
                # waiting on it: there is no sound way to say which is whose.
                # Leave the rows unmatched — they settle terminally and alert
                # below — rather than guess and contaminate a lead.
                unmatched_csv += 1
                _logger.error(
                    "Tracerfy ingest queue %d: %d CSV rows and %d differently-named "
                    "pending rows share one address key — refusing to attribute",
                    queue_id, csv_key_counts[csv_key], len(matches),
                )
                continue
            matched_pending_ids.update(p.id for p in matches)

            phone = csv_row.get("phone")
            email = csv_row.get("email")
            # Multi-contact: up to 3 each (ingest_webhook_csv built these from
            # Mobile-1..5 / Landline-1..3 / Email-1..5). phone/email above are
            # the primary (phones[0]/emails[0]).
            phones = csv_row.get("phones") or []
            emails = csv_row.get("emails") or []
            is_hit = bool(phones or emails)
            if is_hit:
                hit_count += 1
            else:
                miss_count += 1

            # Cache the result for 90-day reuse (Sprint 4).
            # Key off OUR canonical pending-row address (the same fields the READ
            # path in tasks._enqueue_skip_trace_rows hashes), NOT Tracerfy's echoed
            # csv_row address. Tracerfy may USPS-standardize the street (e.g.
            # "St" -> "STREET"), which address_cache_key does NOT collapse, so a
            # csv-keyed write would never match our GIS address on a later run ->
            # 0 cache hits -> the same lead re-paid every scrape. pending_by_key
            # groups by (address, city, state), so all `matches` share this key.
            # PER-TENANT cache (cross-tenant reuse removed 2026-06-10): a Tracerfy
            # batch can contain pending rows from MULTIPLE tenants for the same
            # address, so write one cache row per distinct tenant — each keyed by
            # its own user_id — so a tenant later reuses only ITS OWN result, never
            # another tenant's. All `matches` share the address group, so the
            # address fields come from matches[0]; only user_id varies.
            _addr = matches[0]
            _seen_users: set[str] = set()
            for _pend in matches:
                if _pend.user_id in _seen_users:
                    continue
                _seen_users.add(_pend.user_id)
                cache_key = address_cache_key(
                    _pend.user_id,
                    _addr.property_address or "",
                    _addr.city or "",
                    _addr.state or "",
                )
                existing_cache = db.get(SkipTraceCache, cache_key)
                if existing_cache:
                    existing_cache.phone = phone
                    existing_cache.phone_type = csv_row.get("phone_type")
                    existing_cache.email = email
                    existing_cache.phones = phones
                    existing_cache.emails = emails
                    existing_cache.fetched_at = now
                else:
                    db.add(
                        SkipTraceCache(
                            address_hash=cache_key,
                            phone=phone,
                            phone_type=csv_row.get("phone_type"),
                            phone_dnc_flag=None,
                            email=email,
                            phones=phones,
                            emails=emails,
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
                        phones=phones,
                        emails=emails,
                        skip_trace_status="hit" if is_hit else "miss",
                        skip_trace_attempted_at=now,
                    )
                )
                db.execute(
                    update(PendingSkipTraceRow)
                    .where(PendingSkipTraceRow.id == p.id)
                    .values(status="completed")
                )

        # Settle rows the result CSV never named. Tracerfy finished this batch,
        # so these will never be answered: leaving them 'submitted' stranded the
        # lead on "Processing" indefinitely (confirmed in production on queue
        # 162456). Give them the existing terminal 'errored' state on BOTH the
        # pending row and its Result so the UI shows "Error" and ops can see them.
        #
        # They stay OUT of the billing rollup below (which counts only
        # 'completed'), so the user is not charged for a lookup they never
        # received. That under-bills us against the credits Tracerfy consumed —
        # a deliberate, now-VISIBLE tradeoff rather than the previous silent one.
        unmatched_pending = [p for p in pending if p.id not in matched_pending_ids]
        if unmatched_pending:
            db.execute(
                update(PendingSkipTraceRow)
                .where(PendingSkipTraceRow.id.in_([p.id for p in unmatched_pending]))
                .values(status="errored")
            )
            db.execute(
                update(Result)
                .where(
                    Result.id.in_([p.result_id for p in unmatched_pending]),
                    Result.skip_trace_status.in_(("queued", "submitted")),
                )
                .values(skip_trace_status="errored", skip_trace_attempted_at=now)
            )
            _logger.error(
                "Tracerfy ingest queue %d: %d pending row(s) never appeared in the "
                "result CSV and %d CSV row(s) matched no pending row — marked "
                "'errored'. Sent %d, CSV carried %d.",
                queue_id, len(unmatched_pending), unmatched_csv,
                len(pending), len(parsed),
            )
            _alert_unreconciled(queue_id, len(unmatched_pending), unmatched_csv, len(pending))
        elif unmatched_csv:
            # Rows we did not send but Tracerfy returned. Not lead-affecting, but
            # it means the address key drifted — worth knowing before it grows.
            _logger.warning(
                "Tracerfy ingest queue %d: %d CSV row(s) matched no pending row "
                "(all %d pending rows were still reconciled)",
                queue_id, unmatched_csv, len(pending),
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
        # webhook no longer commits or calls Stripe — it INSERTs a durable
        # SkipTraceMeterEvent outbox row per billable user into THIS same
        # transaction and returns their ids for us to enqueue AFTER commit.
        outbox_ids: list[str] = []
        try:
            from src.api.billing.skip_trace_usage import (
                report_usage_from_webhook,
            )
            billing_summary = report_usage_from_webhook(db, queue_id)
            outbox_ids = billing_summary.get("outbox_ids", [])
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

    # REDTEAM (Codex convergence — meter outbox): the billable MeterEvents are
    # now durably persisted as skip_trace_meter_events outbox rows inside the
    # committed transaction above, so they can no longer be lost. Enqueue each
    # by id as a best-effort fast path; if .delay() raises (broker down at this
    # instant) we only LOG — the flush_skip_trace_meter_outbox beat task sweeps
    # any row still reported_at IS NULL and re-enqueues it. The report task
    # no-ops if reported_at is already set and uses a stable (queue_id,
    # user_id) Stripe identifier, so the event can be neither lost nor
    # double-billed.
    for outbox_id in outbox_ids:
        try:
            report_skip_trace_meter_event.delay(outbox_id)
        except Exception as exc:  # noqa: BLE001 — enqueue failure must not fail ingest
            _logger.error(
                "Failed to enqueue Stripe meter report for queue %d outbox %s: "
                "%s — persisted to outbox, beat sweep will recover it",
                queue_id, outbox_id, str(exc)[:200],
            )

    return {
        "queue_id": queue_id,
        "hits": hit_count,
        "misses": miss_count,
        # Surfaced so a reconciliation gap is visible in the task result and in
        # Flower, not only in a log line nobody is tailing.
        "unmatched_rows": len(unmatched_pending),
        "unmatched_csv_rows": unmatched_csv,
    }
