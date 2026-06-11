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
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import text, update

from src.db.models import BatchRun, Job, ScraperBatch
from src.utils.crypto import decrypt_field
from src.utils.data_exporter import DataExporter
from src.utils.lead_export import write_lead_csv_with_overlap
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


def _filing_sort_key(date_recorded: str | None) -> int:
    """-(ordinal) of 'M/D/YYYY' so most-recent sorts first; blank/garbage -> 0."""
    if not date_recorded:
        return 0
    from datetime import date as _date
    try:
        m, d, y = date_recorded.strip().split("/")
        return -_date(int(y), int(m), int(d)).toordinal()
    except (ValueError, OverflowError):
        return 0


# Combined set over the batch's jobs: dedup by COALESCE(property_key, dedup_hash,
# id), overlap_count = distinct record types within the batch, source_counties
# aggregated. Tenant-scoped (every join carries :uid). Same dedup/ranking as the
# /segments union, scoped to job_ids instead of record_type-over-history.
_COMBINED_SQL = """
WITH candidates AS (
    SELECT r.id, r.date_recorded, r.party_name, r.parcel_id, r.property_address,
           r.mailing_address, r.phone, r.phone_type, r.email,
           r.property_key, r.is_duplicate,
           sc.record_type, sc.county, j.created_at AS job_created_at,
           COALESCE(r.property_key, r.dedup_hash, 'id:' || r.id::text) AS bucket
    FROM results r
    JOIN jobs j ON j.id = r.job_id AND j.user_id = CAST(:uid AS uuid)
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id AND sc.user_id = CAST(:uid AS uuid)
    WHERE r.user_id = CAST(:uid AS uuid)
      AND r.job_id = ANY(CAST(:job_ids AS uuid[]))
),
agg AS (
    SELECT bucket,
           array_agg(DISTINCT record_type ORDER BY record_type) AS matched_record_types,
           count(DISTINCT record_type) AS overlap_count,
           array_agg(DISTINCT county ORDER BY county) AS source_counties
    FROM candidates
    GROUP BY bucket
),
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
SELECT rk.id, rk.date_recorded, rk.party_name, rk.parcel_id, rk.property_address,
       rk.mailing_address, rk.phone, rk.phone_type, rk.email,
       a.matched_record_types, a.overlap_count, a.source_counties
FROM ranked rk
JOIN agg a ON a.bucket = rk.bucket
WHERE rk.rn = 1
LIMIT :limit
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


def _combined_pairs(db, user_id: str, job_ids: list[str]) -> list[tuple]:
    """Return (record_namespace, overlap_dict) pairs for the batch, hottest-first.

    PII (phone/email) is decrypted here — the raw text() query bypasses the
    EncryptedString type. matched_record_types are humanized for the `lists` col.
    """
    if not job_ids:
        return []
    result = db.execute(
        text(_COMBINED_SQL), {"uid": user_id, "job_ids": job_ids, "limit": EXPORT_CAP}
    )
    rows = []
    for r in result.fetchall():
        data = dict(r._mapping)
        if data.get("phone") is not None:
            data["phone"] = decrypt_field(data["phone"])
        if data.get("email") is not None:
            data["email"] = decrypt_field(data["email"])
        rows.append(SimpleNamespace(**data))

    rows.sort(
        key=lambda r: (
            -(r.overlap_count or 0),
            0 if (r.phone or r.email) else 1,
            _filing_sort_key(r.date_recorded),
        )
    )
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


def render_combined_csv(user_id: str, job_ids: list[str]) -> bytes:
    """Build the combined, deduped, overlap-flagged CSV ON DEMAND from the DB
    (NOT the stored R2 snapshot). Used by the download endpoint so:
      - a re-download reflects later async skip-trace fills (fresh contacts), and
      - the API never needs R2 access (R2_ACCOUNT_ID/keys live on the worker, not
        the api — the stored object is only a 'barrier finished' marker).
    Opens its own SYNC psycopg2 session like the worker. Tenant isolation is the
    explicit user_id filter baked into _COMBINED_SQL — callers MUST pass the
    verified owner's id + that batch_run's own child_job_ids.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        pairs = _combined_pairs(db, user_id, job_ids)
        buf = io.StringIO()
        write_lead_csv_with_overlap(pairs, buf)
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

    pairs = _combined_pairs(db, run.user_id, run.child_job_ids or [])

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
                write_lead_csv_with_overlap(pairs, fh)
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
    total_children = len(run.child_job_ids or [])
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

    _deliver(db, run, len(pairs), object_key)
    _logger.info(
        "finalize_batch_run %s: %d leads, status=%s, failed_children=%d",
        run.id, len(pairs), new_status, len(failed),
    )


def _deliver(db, run, lead_count: int, object_key: str | None) -> None:
    """Send the one combined-CSV delivery email (best-effort, non-fatal).

    At-most-once: a re-finalize (lease steal / retry after a crash) must not
    re-send. The status-guarded final write protects DB state, not this
    post-commit email, so we CAS delivery_started_at NULL->now and only the winner
    sends (Codex: duplicate delivery is not tolerable; a missed email is
    recoverable because the CSV is always downloadable in-app)."""
    if not object_key:
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
        from src.workers.delivery import deliver_job_results
        # Link to the in-app batch page (authed streaming download), NOT an R2
        # presigned URL — that S3-presign path 401s in this R2 config (Codex).
        url = f"{settings.FRONTEND_URL.rstrip('/')}/batches/{run.batch_id}"
        deliver_job_results(
            job_id=str(run.id),
            scraper_name=batch.name or "Batch scrape",
            record_count=lead_count,
            download_url=url,
            recipient_emails=emails,
        )
    except Exception as exc:  # delivery is best-effort — the CSV is in R2 either way
        _logger.warning("batch %s delivery email failed: %s", run.id, str(exc)[:200])
