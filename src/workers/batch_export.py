"""Piece 2 Phase 2A.3: batch combined export + delivery.

When all of a batch's child jobs are terminal (done/failed/cancelled), the
completion barrier (scheduler.batch_completion_sweep) claims the run and calls
finalize_batch_run: build ONE combined, deduped, overlap-flagged CSV over the
batch's job_ids (reusing Piece 1's write_lead_csv_with_overlap), upload to R2,
mark the run done/partial, then send one delivery email (best-effort).

Does NOT wait for async skip-trace — contacts (phone/email) fill in later; the
CSV is re-downloadable once they land. The CSV is built on property identity,
which is ready at child-job enrichment.
"""
import io
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select, text, update

from src.api.lead_actionability import actionable_sql
from src.api.tax_filters import TAX_CAP_BIND, tax_cap_min_year, tax_cap_sql
from src.db.models import BatchRun, Job, ScraperBatch
from src.utils.crypto import decrypt_field
from src.utils.data_exporter import DataExporter
from src.utils.lead_export import PROBATE_SUBTYPE_AGG_SQL, write_lead_csv_with_overlap
from src.utils.logger import setup_logger

_logger = setup_logger("worker.batch_export")

EXPORT_CAP = 50_000

# Human-readable list labels (mirror src/api/routes/segments._RECORD_TYPE_LABELS;
# kept local so a worker doesn't import an API route module).
_RECORD_TYPE_LABELS = {
    "probate": "Probate",
    "death_certificate": "Death Certificate",
    "pre_foreclosure": "Pre-Foreclosure",
    "tax_delinquent": "Tax Delinquent",
    "divorce": "Divorce",
    "code_violation": "Code Violation",
    "eviction": "Eviction",
}


def _label(slug: str) -> str:
    return _RECORD_TYPE_LABELS.get(slug, slug.replace("_", " ").title())


# Combined set over the batch's jobs. Dedup bucket (prefixed — the prefixes make
# overlap classification unambiguous and kill cross-key collisions):
#   'pk:' || property_key                      — the ONLY cross-record-type identity
#   'dh:' || record_type || ':' || dedup_hash  — within-type dedup ONLY. dedup_hash's
#       weak branch is party_name+date_recorded (tasks.py), so an un-scoped hash
#       would merge two record types into one fake-overlap row and silently drop
#       one of them (Codex P1).
#   'id:' || id                                — no identity; never groups.
# overlap_count counts DISTINCT record types for pk: buckets only; everything
# else is 1 by construction. Tenant-scoped (every join carries :uid).
# Mode filter (:overlaps_only) and deterministic ORDER BY happen in SQL, BEFORE
# LIMIT/OFFSET — a Python filter after the cap could return zero overlaps even
# when overlaps exist past the 50k sample (Codex P1). Ordering: hottest first
# (overlap_count DESC), then contactable, then newest job, then id (stable).
_COMBINED_CTES = f"""
WITH candidates AS (
    -- Full column set the CSV builder (build_lead_export_row + derive_signals)
    -- consumes — an under-selected set silently blanks populated columns AND, by
    -- omitting delinquent_bill_year, ships a FABRICATED synthetic tax date (the
    -- per-job guard in build_lead_export_row only blanks it when bill_year is
    -- present). phones/emails are decrypted in Python (raw text() bypasses
    -- EncryptedJSON), mirroring segments._decrypt_pii_rows.
    SELECT r.id, r.date_recorded, r.date_recorded_parsed, r.party_name, r.heirs,
           r.parcel_id, r.property_address, r.mailing_address,
           r.property_city, r.property_state, r.property_zip,
           r.legal_description, r.doc_type,
           r.delinquent_amount, r.delinquent_bill_year,
           r.phone, r.phone_type, r.email, r.phones, r.emails,
           r.absentee_owner, r.out_of_state_owner, r.owner_state,
           r.auction_date, r.default_amount, r.enrichment_data,
           r.property_key, r.is_duplicate,
           r.enrichment_data->>'lead_subtype' AS lead_subtype,
           sc.record_type, sc.county, j.created_at AS job_created_at,
           CASE
               WHEN r.property_key IS NOT NULL THEN 'pk:' || r.property_key
               WHEN r.dedup_hash IS NOT NULL
                   THEN 'dh:' || sc.record_type || ':' || r.dedup_hash
               ELSE 'id:' || r.id::text
           END AS bucket
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = CAST(:uid AS uuid)
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
    WHERE r.user_id = CAST(:uid AS uuid)
      AND r.job_id = ANY(CAST(:job_ids AS uuid[]))
      -- Hard 18-month tax-delinquent cap (self-scoping: NULL bill_year rows pass).
      AND {tax_cap_sql('r')}
      -- Standing rule: no property AND no mailing address = not a lead (kept in
      -- results for dedup/health, never delivered or counted). lead_actionability.
      AND {actionable_sql('r')}
),
agg AS (
    SELECT bucket,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           CASE WHEN bucket LIKE 'pk:%' THEN count(DISTINCT record_type) ELSE 1 END AS overlap_count,
           {PROBATE_SUBTYPE_AGG_SQL},
           -- Coalesce the tax-delinquency fields ACROSS the bucket (like the probate
           -- subtype above): a cross-matched property's representative row is usually
           -- the PROBATE row (it carries the owner name, so it is the one skip-trace
           -- reaches and rn=1 picks), which leaves delinquent_amount/bill_year NULL.
           -- The tax figures live on the sibling tax_delinquent row. max() over the
           -- bucket lifts them onto the combined lead so it shows BOTH the death cert
           -- AND the delinquency (the whole point of a combined lead). Single-source
           -- by nature (only tax rows populate them), so max() can't cross-contaminate.
           max(delinquent_amount) AS delinquent_amount,
           max(delinquent_bill_year) AS delinquent_bill_year,
           array_agg(DISTINCT county ORDER BY county) AS source_counties
    FROM candidates
    GROUP BY bucket
)"""

_COMBINED_SQL = _COMBINED_CTES + """,
ranked AS (
    SELECT c.*,
           row_number() OVER (
               PARTITION BY c.bucket
               ORDER BY (CASE WHEN c.phone IS NOT NULL OR c.email IS NOT NULL
                              THEN 0 ELSE 1 END),
                        c.is_duplicate ASC,
                        c.job_created_at DESC NULLS LAST,
                        c.id DESC
           ) AS rn
    FROM candidates c
)
SELECT rk.id, rk.date_recorded, rk.date_recorded_parsed, rk.party_name, rk.heirs,
       rk.parcel_id, rk.property_address, rk.mailing_address,
       rk.property_city, rk.property_state, rk.property_zip,
       rk.legal_description, rk.doc_type,
       -- Bucket-coalesced (agg), NOT the representative row's own — see agg CTE.
       a.delinquent_amount, a.delinquent_bill_year,
       rk.phone, rk.phone_type, rk.email, rk.phones, rk.emails,
       rk.absentee_owner, rk.out_of_state_owner, rk.owner_state,
       rk.auction_date, rk.default_amount, rk.enrichment_data,
       -- The representative row's own type — lets the CSV builder tell a real date
       -- from the synthetic tax date after the tax bill_year is coalesced in (Codex).
       rk.record_type,
       a.matched_record_types, a.overlap_count, a.source_counties, a.lead_subtype
FROM ranked rk
JOIN agg a ON a.bucket = rk.bucket
WHERE rk.rn = 1
  AND (NOT :overlaps_only OR (rk.bucket LIKE 'pk:%' AND a.overlap_count >= 2))
ORDER BY a.overlap_count DESC,
         (CASE WHEN rk.phone IS NOT NULL OR rk.email IS NOT NULL THEN 0 ELSE 1 END),
         rk.job_created_at DESC NULLS LAST,
         rk.id DESC
LIMIT :limit OFFSET :offset
"""

# Honest delivery accounting over the SAME dedup/aggregation — UNCAPPED (counts
# must be batch facts, not capped-sample facts — Codex P1). Mode-independent:
# these are dataset facts; delivery interprets them per mode.
_DELIVERY_COUNTS_SQL = _COMBINED_CTES + """
SELECT count(*) AS leads_total,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count >= 2) AS overlaps_delivered,
       count(*) FILTER (WHERE bucket LIKE 'pk:%' AND overlap_count < 2) AS singletons_suppressed,
       count(*) FILTER (WHERE bucket NOT LIKE 'pk:%') AS unmatchable_no_parcel
FROM agg
"""


# Per-child terminal status for the failed_children summary. Same uuid-param
# casting as _COMBINED_SQL (raw text() binds str/list[str] as text/text[]; the
# columns are native uuid — psycopg2 has no uuid=text operator without the cast).
_FAILED_CHILDREN_SQL = """
SELECT j.id::text AS job_id, j.status AS status,
       sc.county AS county, sc.record_type AS record_type
FROM jobs j
JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
WHERE j.user_id = CAST(:uid AS uuid) AND j.id = ANY(CAST(:job_ids AS uuid[]))
"""


_EMPTY_COUNTS = {
    "leads_total": 0,
    "overlaps_delivered": 0,
    "singletons_suppressed": 0,
    "unmatchable_no_parcel": 0,
}


def _combined_pairs(
    db,
    user_id: str,
    job_ids: list[str],
    delivery_mode: str = "everything",
    limit: int | None = EXPORT_CAP,
    offset: int = 0,
) -> list[tuple]:
    """Return (record_namespace, overlap_dict) pairs for the batch, hottest-first.

    Ordering + mode filtering are SQL-side (deterministic; pagination-safe).
    `limit=None` binds SQL `LIMIT NULL` (unbounded — the whole set in one snapshot).
    PII (phone/email) is decrypted here — the raw text() query bypasses the
    EncryptedString type. matched_record_types are humanized for the `lists` col.
    """
    if not job_ids:
        return []
    result = db.execute(
        text(_COMBINED_SQL),
        {
            "uid": user_id,
            "job_ids": job_ids,
            "limit": limit,
            "offset": offset,
            "overlaps_only": delivery_mode == "overlaps_only",
            TAX_CAP_BIND: tax_cap_min_year(datetime.now(UTC).date()),
        },
    )
    rows = []
    for r in result.fetchall():
        data = dict(r._mapping)
        if data.get("phone") is not None:
            data["phone"] = decrypt_field(data["phone"])
        if data.get("email") is not None:
            data["email"] = decrypt_field(data["email"])
        # Multi-contact arrays (EncryptedJSON over raw text()) — decrypt + parse so
        # phone_2/3 + email_2/3 populate, exactly like segments._decrypt_pii_rows.
        # Unparseable → None (CSV then emits blank secondaries, never a 500).
        for key in ("phones", "emails"):
            raw = data.get(key)
            if raw is None:
                continue
            try:
                data[key] = json.loads(decrypt_field(raw))
            except (ValueError, TypeError):
                data[key] = None
        # The winning row's own enrichment_data carries a lead_subtype, but the
        # bucket-AGGREGATED subtype (a.lead_subtype via PROBATE_SUBTYPE_AGG_SQL) is
        # authoritative — a bucket's representative row may be non-probate. Drop the
        # per-row subtype from a COPY (never mutate the fetched row) so
        # build_lead_export_row falls back to the aggregated scalar (Codex P2).
        enr = data.get("enrichment_data")
        if isinstance(enr, dict) and "lead_subtype" in enr:
            data["enrichment_data"] = {k: v for k, v in enr.items() if k != "lead_subtype"}
        rows.append(SimpleNamespace(**data))
    return [
        (
            r,
            {
                "lists_count": r.overlap_count,
                "lists": "; ".join(_label(t) for t in (r.matched_record_types or [])),
                "counties": "; ".join(r.source_counties or []),
            },
        )
        for r in rows
    ]


def _combined_pairs_all(
    db, user_id: str, job_ids: list[str], delivery_mode: str = "everything"
) -> list[tuple]:
    """ALL combined pairs for the delivered CSV, in ONE query (limit=None -> SQL
    `LIMIT NULL` = unbounded). Two properties we need together:
      - no SILENT truncation: the old default limit=EXPORT_CAP dropped rows past 50k
        while the email/UI counts reported the true, larger total (a broken paid
        export).
      - a CONSISTENT snapshot: a single statement reads one MVCC snapshot, so a
        skip-trace contact fill landing mid-read can't reorder rows across pages and
        duplicate/drop them the way OFFSET paging could (Codex P2 — the ORDER BY
        keys on contactable status, which async enrichment mutates).
    EXPORT_CAP stays the API page size for the interactive /leads view; this
    delivered-export path is intentionally uncapped."""
    return _combined_pairs(db, user_id, job_ids, delivery_mode=delivery_mode, limit=None)


def compute_delivery_counts(db, user_id: str, job_ids: list[str]) -> dict[str, int]:
    """Uncapped, mode-independent dataset facts for honest delivery messaging."""
    if not job_ids:
        return dict(_EMPTY_COUNTS)
    row = db.execute(
        text(_DELIVERY_COUNTS_SQL),
        {
            "uid": user_id,
            "job_ids": job_ids,
            TAX_CAP_BIND: tax_cap_min_year(datetime.now(UTC).date()),
        },
    ).one()
    return {
        "leads_total": int(row.leads_total),
        "overlaps_delivered": int(row.overlaps_delivered),
        "singletons_suppressed": int(row.singletons_suppressed),
        "unmatchable_no_parcel": int(row.unmatchable_no_parcel),
    }


def render_combined_csv(
    user_id: str,
    job_ids: list[str],
    hidden_fields: set[str] | None = None,
    delivery_mode: str = "everything",
) -> bytes:
    """Build the combined, deduped, overlap-flagged CSV ON DEMAND from the DB
    (NOT the stored R2 snapshot). Used by the download endpoint so:
      - a re-download reflects later async skip-trace fills (fresh contacts), and
      - the API never needs R2 access (R2_ACCOUNT_ID/keys live on the worker, not
        the api — the stored object is only a 'barrier finished' marker).
    Opens its own SYNC psycopg2 session like the worker. Tenant isolation is the
    explicit user_id filter baked into _COMBINED_SQL — callers MUST pass the
    verified owner's id + that batch_run's own child_job_ids.

    `hidden_fields` (from the batch's shared `fields`) blanks the user-deselected
    hideable columns so the combined download matches the per-job exports.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        pairs = _combined_pairs_all(db, user_id, job_ids, delivery_mode=delivery_mode)
        buf = io.StringIO()
        write_lead_csv_with_overlap(pairs, buf, hidden_fields=hidden_fields)
        db.rollback()  # read-only
        return buf.getvalue().encode("utf-8")


def finalize_batch_run(db, run, forced: bool = False, claim_token: str | None = None) -> None:
    """Build + upload the combined CSV and deliver it. Called AFTER the run is
    claimed (lease held) and either all child jobs are terminal OR (forced=True)
    the run is past the hard deadline. Commits the run status before emailing so a
    delivery failure can't undo the export.

    forced=True (Track A backstop): a run stuck 'running' past the deadline
    finalizes anyway — missing / still-non-terminal children count as failed and
    the combined CSV is built from whatever children DID produce results, so a
    permanently-stuck child can't strand the run forever.

    claim_token (Track A): the lease owner token. If a finalize runs longer than
    the lease TTL, another sweep can re-claim the run and run a concurrent
    finalize. Guarding the terminal write on claim_token ensures only the CURRENT
    lease owner commits the status + triggers delivery — a stale worker's write
    no-ops (Codex P2). The R2 overwrite is idempotent (same key, deterministic
    content), so a double upload is harmless."""
    # Per-child status -> failed_children summary.
    child_rows = db.execute(
        text(_FAILED_CHILDREN_SQL),
        {"uid": run.user_id, "job_ids": run.child_job_ids or []},
    ).fetchall()
    if forced:
        # Any child not 'done' (failed/cancelled OR still non-terminal) is a
        # failure; children missing from the table entirely are 'missing'.
        present = {row.job_id for row in child_rows}
        failed = [
            {
                "job_id": row.job_id,
                "county": row.county,
                "record_type": row.record_type,
                "reason": row.status if row.status in ("failed", "cancelled") else "timed out",
            }
            for row in child_rows
            if row.status != "done"
        ]
        failed += [
            {"job_id": jid, "county": None, "record_type": None, "reason": "missing"}
            for jid in (run.child_job_ids or [])
            if jid not in present
        ]
        # Terminalize still-active children (Codex P2): force-finalize only RECORDS
        # them as timed out, but the underlying jobs row stays pending/scraping. A
        # lost child .delay() would leave a permanent active job, and a delayed
        # broker message could claim + scrape + bill it AFTER the batch is terminal.
        # Cancel them in this same txn so run_scrape_job's boot check / atomic claim
        # short-circuits any late delivery (idempotent: only non-terminal rows flip).
        db.execute(
            update(Job)
            .where(
                Job.id.in_(run.child_job_ids or []),
                Job.user_id == run.user_id,
                Job.status.not_in(("done", "failed", "cancelled")),
            )
            .values(status="cancelled", finished_at=datetime.now(UTC))
        )
    else:
        failed = [
            {"job_id": row.job_id, "county": row.county, "record_type": row.record_type, "reason": row.status}
            for row in child_rows
            if row.status in ("failed", "cancelled")
        ]

    # The parent batch owns output shape: delivery_mode (what the combined
    # export contains) + fields (hideable-column visibility).
    from src.utils.lead_export import resolve_hidden_output_fields
    _batch = db.get(ScraperBatch, run.batch_id)
    delivery_mode = (_batch.delivery_mode if _batch else None) or "everything"
    hidden_fields = resolve_hidden_output_fields(_batch.fields if _batch else None)

    pairs = _combined_pairs_all(
        db, run.user_id, run.child_job_ids or [], delivery_mode=delivery_mode
    )
    # Honest accounting (uncapped, mode-independent). Stored on the run as the
    # as-delivered snapshot; live reads recompute with the current mode.
    counts = compute_delivery_counts(db, run.user_id, run.child_job_ids or [])

    object_key = None
    if pairs:
        exporter = DataExporter()
        # UNIQUE local temp name per finalize (Codex P2): if a finalize outruns its
        # lease and another reclaims, two finalizers can run at once. A shared name
        # would let one overwrite/unlink the other's file mid-upload. A per-call
        # suffix (claim_token if present, else a uuid) isolates them; the R2 object
        # key stays stable (idempotent overwrite of equivalent content).
        suffix = (claim_token or uuid.uuid4().hex)[:8]
        local_path = exporter.export_dir / f"batch_{run.id[:8]}_{suffix}.csv"
        exporter.export_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(local_path, "w", newline="", encoding="utf-8") as fh:
                write_lead_csv_with_overlap(pairs, fh, hidden_fields=hidden_fields)
            object_key = exporter.upload_to_r2(
                local_path, f"exports/{run.user_id}/batch/{run.id}/combined.csv"
            )
        finally:
            local_path.unlink(missing_ok=True)

    # STATUS-GUARDED final write (Codex P1): only flip running->done/partial if the
    # run is STILL 'running'. A cancel that committed during the CSV/R2 build (after
    # the claim) makes rowcount 0 -> we must NOT overwrite 'cancelled' or deliver.
    # done = all succeeded; failed = ALL children failed; partial = a mix
    # (Codex: 100%-failure must not read as 'partial').

    # Configs blocked by tier enforcement were recorded on the run at fan-out time;
    # they never became child jobs, so child_rows can't see them. Read fresh (avoid
    # identity-map staleness) and merge so the run reads partial/failed correctly and
    # blocked configs stay visible in failed_children. Blocked entries have no job_id,
    # so there's no overlap with the per-child `failed` list.
    prior_blocked = db.execute(
        select(BatchRun.failed_children).where(BatchRun.id == run.id)
    ).scalar() or []
    failed = prior_blocked + failed

    total_children = len(run.child_job_ids or []) + len(prior_blocked)
    if not failed:
        new_status = "done"
    elif len(failed) >= total_children and total_children > 0:
        new_status = "failed"
    else:
        new_status = "partial"
    # LEASE-OWNER guard (Codex P2): if this finalize outran its lease and another
    # sweep re-claimed the run, claim_token has changed -> rowcount 0 -> we don't
    # commit a terminal state or deliver. Only the current owner finalizes.
    guard = [BatchRun.id == run.id, BatchRun.status == "running"]
    if claim_token is not None:
        guard.append(BatchRun.claim_token == claim_token)
    updated = db.execute(
        update(BatchRun)
        .where(*guard)
        .values(
            combined_export_key=object_key,
            failed_children=failed or None,
            status=new_status,
            completed_at=datetime.now(UTC),
            delivery_counts=counts,
        )
    ).rowcount
    db.commit()
    if not updated:
        _logger.info(
            "finalize_batch_run %s: not finalized (cancelled, or lease re-claimed "
            "by another worker) — no delivery",
            run.id,
        )
        return

    _deliver(
        db, run, len(pairs), object_key,
        new_status=new_status,
        summary=_delivery_summary(delivery_mode, counts),
    )
    _logger.info(
        "finalize_batch_run %s: %d leads, status=%s, failed_children=%d",
        run.id, len(pairs), new_status, len(failed),
    )


def _delivery_summary(mode: str, counts: dict) -> str:
    """One honest sentence for the delivery email. The empty overlaps_only case
    must read as 'no overlaps found' (with the why), never as 'broken'."""
    total = counts.get("leads_total", 0)
    overlaps = counts.get("overlaps_delivered", 0)
    singletons = counts.get("singletons_suppressed", 0)
    no_parcel = counts.get("unmatchable_no_parcel", 0)
    if mode == "overlaps_only":
        if overlaps == 0:
            return (
                f"0 cross-list overlap leads found across {total:,} scraped leads. "
                f"{no_parcel:,} lead(s) had no parcel number and couldn't be "
                "cross-matched. Switch the batch to 'Everything' to receive all leads."
            )
        # Report singletons and unmatchable-no-parcel SEPARATELY — lumping the
        # no-parcel rows into "single-list" (total - overlaps) mislabels them.
        parts = [f"{overlaps:,} lead(s) found on 2 or more lists."]
        if singletons:
            parts.append(f"{singletons:,} single-list lead(s) not included in this delivery.")
        if no_parcel:
            parts.append(
                f"{no_parcel:,} lead(s) had no parcel number and couldn't be cross-matched."
            )
        return " ".join(parts)
    return f"{overlaps:,} of {total:,} lead(s) appear on 2 or more lists."


def _deliver(
    db, run, lead_count: int, object_key: str | None,
    new_status: str = "done", summary: str | None = None,
) -> None:
    """Send the one combined-CSV delivery email (best-effort, non-fatal),
    including the honest delivery summary message.

    At-most-once: a re-finalize (lease steal / retry after a crash) must not
    re-send. The status-guarded final write protects DB state, not this
    post-commit email, so we CAS delivery_started_at NULL->now and only the winner
    sends (Codex: duplicate delivery is not tolerable; a missed email is
    recoverable because the CSV is always downloadable in-app)."""
    # Email on every successful finalize — including a zero-row overlaps_only
    # run (the honest empty-state IS the delivery). Fully-failed runs don't
    # email (ops alerts cover failures); the old `if not object_key` gate would
    # have silently skipped exactly the empty-state case this feature exists for.
    if new_status not in ("done", "partial"):
        return
    claimed = db.execute(
        update(BatchRun)
        .where(BatchRun.id == run.id, BatchRun.delivery_started_at.is_(None))
        .values(delivery_started_at=datetime.now(UTC))
    ).rowcount
    db.commit()
    if not claimed:
        _logger.info(
            "batch %s: delivery already started/sent — skipping duplicate email", run.id
        )
        return
    batch = db.get(ScraperBatch, run.batch_id)
    if batch is None:
        return
    deliver = batch.deliver or {}
    emails = deliver.get("emails") or []
    if not emails:
        return
    try:
        from src.config import settings
        from src.workers.delivery import deliver_job_email
        # Link to the in-app batch page (authed streaming download), NOT an R2
        # presigned URL — that S3-presign path 401s in this R2 config (Codex).
        url = f"{settings.FRONTEND_URL.rstrip('/')}/batches/{run.batch_id}"
        # Enqueue on Celery so a transient Resend failure is retried instead of
        # dropped. The delivery_started_at CAS above already guarantees this runs
        # at most once per batch, so enqueue-once + retry-on-failure is safe.
        deliver_job_email.delay(
            job_id=str(run.id),
            scraper_name=batch.name or "Batch scrape",
            record_count=lead_count,
            download_url=url,
            recipient_emails=emails,
            summary_message=summary,
            link_expires=False,  # in-app batch page — not a presigned URL (Codex P2)
        )
    except Exception as exc:  # enqueue is best-effort — the CSV is in R2 either way
        # The delivery_started_at CAS above is already consumed, so no future
        # finalizer will retry this batch email — surface the miss to ops (the
        # per-job path alerts on the same failure mode).
        _logger.warning("batch %s delivery email enqueue failed: %s", run.id, str(exc)[:200])
        try:
            from src.workers.ops_alerts import send_ops_alert
            send_ops_alert(
                "batch_email_enqueue", str(run.id),
                "Batch lead email could not be queued",
                f"Could not queue the delivery email for batch run {run.id}: "
                f"{str(exc)[:200]}. The combined export is available in-app.",
            )
        except Exception:  # ops alert is best-effort — never mask the original miss
            pass
