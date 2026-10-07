"""Account data export (P4): build each requested ZIP, email its link, expire it.

Runs every minute as bridgeleads_system, one run at a time (advisory lock), and is the
ONLY builder: the route just inserts a pending account_exports row (migration 115), so
every claim goes through the one conditional UPDATE below. Design + review log:
docs/product/account-deletion-and-export.md §3, tasks/todo-account-deletion.md (P4).

  pending  -> building  claimed with a lease (rotates claim_id; attempts + 1). A crashed
                        build's lease runs out and the row is claimed again.
  building -> ready     ZIP uploaded to exports/{user_id}/account/{id}.zip (inside the
                        P3 purge sweep), then published only while the owner has no
                        deletion request: the users row is read FOR SHARE, which waits
                        for (and blocks) request_account_deletion's FOR NO KEY UPDATE.
                        So an export is downloadable only if it was ready before any
                        deletion request (owner decision A, 2026-10-07).
  building -> failed    deletion requested / too large / third failed attempt; the
                        uploaded object, if any, is deleted first.
  ready                 the 7-day link is emailed to the account's CURRENT address; a
                        failed send is retried on its own and never un-readies it.
  ready -> expired      after 7 days the object is deleted.

The ZIP: profile.json, scrapers.json, batches.json, runs.json and leads/<job id>.csv for
every finished job whose file the user can download today, built by the SAME query and
CSV builder as GET /jobs/{id}/download (CSV-injection sanitised). All reads in one
REPEATABLE READ snapshot, every query filtered by user_id. Member names are ids only.
"""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.account_export")

# One run at a time. Arbitrary constant, unique in this codebase.
_LOCK_KEY = 7_115_000_001
# Longer than the task's hard time limit (scheduler.py), so a killed build's lease
# always runs out after its worker is gone.
_LEASE = "interval '30 minutes'"
_MAX_ATTEMPTS = 3
_EMAIL_MAX_ATTEMPTS = 5
_BATCH = 50

_README = """BridgeLeads data export

profile.json    your account details
scrapers.json   your scrapers: counties, record types, fields, schedules, delivery
                settings (webhook secrets and access tokens are never included)
batches.json    your batch scrapes
runs.json       every run, with the lead file it produced (lead_file)
leads/          one CSV per finished run: the same file its Download button gives you

This file contains personal information about the people in your leads. Keep it safe.
"""


def export_key(user_id: str, export_id: str) -> str:
    """Derived, never stored (migration 115): a row cannot point at another object."""
    return f"exports/{user_id}/account/{export_id}.zip"


class ExportStore:
    """The export objects in R2. Tests pass an in-memory stand-in."""

    def upload(self, local_path: Path, key: str) -> None:
        from src.utils.data_exporter import DataExporter
        DataExporter().upload_to_r2(local_path, key)

    def delete(self, key: str) -> bool:
        from src.utils.data_exporter import DataExporter
        return DataExporter().delete_from_r2(key)


class _TooLarge(Exception):
    pass


# ── Building the ZIP ─────────────────────────────────────────────────────────

def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _profile(user) -> dict:
    """An allowlist: never a hash, key, token or Stripe id."""
    return {c: getattr(user, c) for c in (
        "email", "first_name", "last_name", "name", "timezone", "plan",
        "subscription_status", "trial_ends_at", "records_used", "records_limit",
        "referral_code", "notification_prefs", "mfa_enabled", "created_at",
    )}


def _batch(batch) -> dict:
    from src.api.schemas import DELIVER_SECRET_FIELDS

    deliver = batch.deliver if isinstance(batch.deliver, dict) else {}
    return {
        "id": batch.id, "name": batch.name, "state": batch.state, "status": batch.status,
        "delivery_mode": batch.delivery_mode, "fields": batch.fields,
        "enrichment": batch.enrichment, "schedule": batch.schedule,
        "deliver": {k: v for k, v in deliver.items() if k not in DELIVER_SECRET_FIELDS},
        "created_at": batch.created_at,
    }


def _build_zip(user_id: str, zip_path: Path) -> None:
    """Write the account's ZIP to zip_path. Raises _TooLarge over the row cap."""
    from src.api.results_category import download_rows_select
    from src.api.schemas import ScraperConfigResponse
    from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
    from src.db.session import rls_sync_session
    from src.utils.data_exporter import DataExporter
    from src.utils.lead_export import config_export_options

    exporter = DataExporter()
    today = datetime.now(UTC).date()
    with rls_sync_session(user_id) as db, zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        db.commit()  # ends the GUC transaction; the next one is the snapshot
        db.connection(execution_options={
            "isolation_level": "REPEATABLE READ", "postgresql_readonly": True})
        user = db.get(User, user_id)
        configs = db.scalars(select(ScraperConfig).where(ScraperConfig.user_id == user_id)
                             .order_by(ScraperConfig.created_at)).all()
        batches = db.scalars(select(ScraperBatch).where(ScraperBatch.user_id == user_id)
                             .order_by(ScraperBatch.created_at)).all()
        jobs = db.scalars(select(Job).where(Job.user_id == user_id)
                          .order_by(Job.created_at)).all()
        batch_runs = db.scalars(select(BatchRun).where(BatchRun.user_id == user_id)
                                .order_by(BatchRun.created_at)).all()
        by_id = {c.id: c for c in configs}

        zf.writestr("README.txt", _README)
        zf.writestr("profile.json", _json(_profile(user)))
        zf.writestr("scrapers.json", _json(
            [ScraperConfigResponse.model_validate(c).model_dump(mode="json") for c in configs]))
        zf.writestr("batches.json", _json([_batch(b) for b in batches]))

        runs, total = [], 0
        for job in jobs:
            cfg = by_id.get(job.scraper_config_id)
            lead_file = None
            # The rule GET /jobs/{id}/download applies: a finished run with a file.
            if job.status == "done" and job.export_key:
                rows = db.scalars(download_rows_select(job.id, user_id, today)).all()
                total += len(rows)
                if total > settings.ACCOUNT_EXPORT_MAX_ROWS:
                    raise _TooLarge
                if rows:
                    path = exporter.export(rows, filename=f"acct_{job.id[:8]}", fmt="csv",
                                           **config_export_options(cfg))
                    try:
                        lead_file = f"leads/{job.id}.csv"
                        zf.write(path, lead_file)
                    finally:
                        path.unlink(missing_ok=True)
            runs.append({
                "id": job.id, "scraper_config_id": job.scraper_config_id,
                "scraper_name": cfg.name if cfg else None,
                "county": cfg.county if cfg else None,
                "record_type": cfg.record_type if cfg else None,
                "status": job.status, "trigger": job.trigger,
                "record_count": job.record_count, "billed_count": job.billed_count,
                "date_from": job.date_from, "date_to": job.date_to,
                "created_at": job.created_at, "started_at": job.started_at,
                "finished_at": job.finished_at, "lead_file": lead_file,
            })
        zf.writestr("runs.json", _json({
            "runs": runs,
            "batch_runs": [{
                "id": r.id, "batch_id": r.batch_id, "status": r.status,
                "child_job_ids": r.child_job_ids, "created_at": r.created_at,
                "completed_at": r.completed_at,
            } for r in batch_runs],
        }))
        db.rollback()


# ── The state machine ────────────────────────────────────────────────────────

def _claim(db):
    row = db.execute(text(f"""
        UPDATE account_exports e
           SET status = 'building', claim_id = gen_random_uuid(),
               claimed_until = now() + {_LEASE}, attempts = e.attempts + 1,
               next_attempt_at = NULL
         WHERE e.id = (
               SELECT id FROM account_exports
                WHERE (status = 'pending' OR (status = 'building' AND claimed_until < now()))
                  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                ORDER BY requested_at
                LIMIT 1 FOR UPDATE SKIP LOCKED)
     RETURNING e.id, e.user_id, e.claim_id, e.attempts
    """)).one_or_none()  # noqa: S608 - _LEASE is a fixed literal
    db.commit()
    return row


_HOLDS_CLAIM = ("id = :id AND claim_id = :claim AND status = 'building' "
                "AND claimed_until > now()")


def _fail(db, row, store: ExportStore, code: str) -> None:
    """Give up on this export (the row stays as the request log), then delete anything
    it uploaded. Only while this run still holds the claim: a run that lost it must
    not delete the object of the run that holds it now."""
    failed = db.execute(text(
        "UPDATE account_exports SET status = 'failed', last_error = :e, "  # noqa: S608
        f"claim_id = NULL, claimed_until = NULL WHERE {_HOLDS_CLAIM}"),
        {"id": row.id, "claim": row.claim_id, "e": code}).rowcount
    db.commit()
    if failed and not store.delete(export_key(row.user_id, row.id)):
        # Under exports/{user_id}/: the account purge sweeps it if nothing else does.
        _logger.error("account export %s: could not delete its object", row.id)


def _retry_later(db, row, store: ExportStore, code: str) -> None:
    if row.attempts >= _MAX_ATTEMPTS:
        _fail(db, row, store, code)
        return
    db.execute(text(
        "UPDATE account_exports SET status = 'pending', claim_id = NULL, "  # noqa: S608
        "claimed_until = NULL, last_error = :e, "
        "next_attempt_at = now() + make_interval(mins => 5 * attempts) "
        f"WHERE {_HOLDS_CLAIM}"), {"id": row.id, "claim": row.claim_id, "e": code})
    db.commit()


def _owner_state(db, user_id: str, lock: bool = False):
    sql = "SELECT deletion_state, is_active FROM users WHERE id = :u"
    return db.execute(text(sql + (" FOR SHARE" if lock else "")), {"u": user_id}).one()


def _build_one(db, row, store: ExportStore) -> str:
    owner = _owner_state(db, row.user_id)
    db.rollback()
    if owner.deletion_state is not None or not owner.is_active:
        _fail(db, row, store, "deletion_requested")
        return "failed"
    if row.attempts > _MAX_ATTEMPTS:  # crashed or was killed every time
        _fail(db, row, store, "build_failed")
        return "failed"

    from src.utils.data_exporter import DataExporter

    zip_path = DataExporter().export_dir / f"account_{row.id}.zip"
    key = export_key(row.user_id, row.id)
    try:
        try:
            _build_zip(row.user_id, zip_path)
        except _TooLarge:
            _fail(db, row, store, "too_large")
            return "failed"
        except Exception:  # noqa: BLE001 - logged with the id only; retried with backoff
            _logger.exception("account export %s: build failed", row.id)
            _retry_later(db, row, store, "build_failed")
            return "retry"
        size = zip_path.stat().st_size
        if size > settings.ACCOUNT_EXPORT_MAX_BYTES:
            _fail(db, row, store, "too_large")
            return "failed"
        try:
            store.upload(zip_path, key)
        except Exception:  # noqa: BLE001 - logged with the id only; retried with backoff
            _logger.exception("account export %s: upload failed", row.id)
            _retry_later(db, row, store, "upload_failed")
            return "retry"
    finally:
        zip_path.unlink(missing_ok=True)

    # Publish, ordered against a deletion request on the users row.
    owner = _owner_state(db, row.user_id, lock=True)
    if owner.deletion_state is not None or not owner.is_active:
        db.rollback()
        _fail(db, row, store, "deletion_requested")
        return "failed"
    published = db.execute(text(
        "UPDATE account_exports SET status = 'ready', size_bytes = :s, ready_at = now(), "  # noqa: S608
        "expires_at = now() + interval '7 days', claim_id = NULL, claimed_until = NULL, "
        f"last_error = NULL, next_attempt_at = NULL WHERE {_HOLDS_CLAIM}"),
        {"id": row.id, "claim": row.claim_id, "s": size}).rowcount
    db.commit()
    if not published:
        # The lease ran out and another run owns the row now; it rebuilds the same key.
        _logger.warning("account export %s: lost its claim before publishing", row.id)
        return "lost"
    return "ready"


def _export_ready_notice(link: str, expires_at: datetime) -> tuple:
    """(subject, preheader, lines, cta) of the "your export is ready" email."""
    when = expires_at.astimezone(UTC).strftime("%B %d, %Y")
    return (
        "Your BridgeLeads data export is ready",
        f"Download it before {when}.",
        ["The data export you asked for is ready. It is one ZIP file with your profile, "
         "scrapers, schedules, run history and every lead file you can download today.",
         f"The link works until {when}. You can also download it from Settings > Account.",
         "The file contains personal information about the people in your leads. "
         "Keep it somewhere safe."],
        ("Download your data", link),
    )


def _send_export_ready(to: str, link: str, expires_at: datetime) -> None:
    """Raises on failure: the caller backs off and retries."""
    from src.workers.account_emails import _send

    subject, preheader, lines, cta = _export_ready_notice(link, expires_at)
    _send(to, subject, preheader, lines, cta=cta)


def _send_links(db, send) -> int:
    if not settings.RESEND_API_KEY or not settings.API_BASE_URL:
        # A config error, not a delivery failure: leave the emails owed.
        _logger.error("RESEND_API_KEY or API_BASE_URL unset: export emails left pending")
        return 0
    from src.api.download_tokens import mint_account_export_token
    from src.db.models import User
    from src.workers.delivery import _email_error_summary

    rows = db.execute(text("""
        SELECT e.id, e.user_id, e.expires_at FROM account_exports e
          JOIN users u ON u.id = e.user_id
         WHERE e.status = 'ready' AND e.email_sent_at IS NULL
           AND e.email_attempts < :max AND e.expires_at > now() + interval '1 hour'
           AND u.deletion_state IS NULL AND u.is_active
           AND (e.next_attempt_at IS NULL OR e.next_attempt_at <= now())
         ORDER BY e.ready_at LIMIT :n
    """), {"max": _EMAIL_MAX_ATTEMPTS, "n": _BATCH}).all()
    sent = 0
    for row in rows:
        email = db.get(User, row.user_id).email  # ORM: decrypted, the CURRENT address
        db.rollback()
        ttl = int((row.expires_at - datetime.now(UTC)).total_seconds())
        token = mint_account_export_token(str(row.user_id), str(row.id), ttl_seconds=ttl)
        link = f"{settings.API_BASE_URL.rstrip('/')}/auth/export/{row.id}/download?token={token}"
        try:
            send(email, link, row.expires_at)
        except Exception as exc:  # noqa: BLE001 - recorded and retried with backoff
            _logger.warning("account export %s: email failed: %s", row.id,
                            _email_error_summary(exc))
            db.execute(text(
                "UPDATE account_exports SET email_attempts = email_attempts + 1, "
                "next_attempt_at = now() + make_interval(mins => 5 * (email_attempts + 1)) "
                "WHERE id = :id AND status = 'ready'"), {"id": row.id})
            db.commit()
            continue
        db.execute(text("UPDATE account_exports SET email_sent_at = now(), "
                        "next_attempt_at = NULL WHERE id = :id AND email_sent_at IS NULL"),
                   {"id": row.id})
        db.commit()
        sent += 1
    return sent


def _expire(db, store: ExportStore) -> int:
    """Delete objects whose 7 days are up (the download route refuses them already)."""
    rows = db.execute(text(
        "SELECT id, user_id FROM account_exports WHERE status = 'ready' "
        "AND expires_at <= now() ORDER BY expires_at LIMIT :n"), {"n": _BATCH}).all()
    db.rollback()
    expired = 0
    for row in rows:
        if not store.delete(export_key(row.user_id, row.id)):
            continue  # kept 'ready' (and refused by the route): retried next run
        db.execute(text("UPDATE account_exports SET status = 'expired' "
                        "WHERE id = :id AND status = 'ready'"), {"id": row.id})
        db.commit()
        expired += 1
    return expired


def _build_account_exports_impl(*, store: ExportStore | None = None, send=None) -> dict:
    from src.db.session import system_sync_session

    store = store or ExportStore()
    with system_sync_session() as lock_db:
        if not lock_db.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": _LOCK_KEY}).scalar():
            _logger.info("account export beat: another run holds the lock")
            return {"skipped": True}
        try:
            with system_sync_session() as db:
                # ponytail: one build per tick (every minute); queue several per tick if
                # exports ever back up.
                row = _claim(db)
                built = _build_one(db, row, store) if row else None
                return {
                    "built": built,
                    "emails": _send_links(db, send or _send_export_ready),
                    "expired": _expire(db, store),
                }
        finally:
            lock_db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock_db.commit()
