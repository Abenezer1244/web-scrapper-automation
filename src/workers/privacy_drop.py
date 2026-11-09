"""California DROP list pulls (P9 foundation): download, parse, record. Nothing else.

pull_drop_lists (beat, daily; a pull at most every 7 days)
  Does nothing unless DROP_ENABLED is true and DROP_API_KEY is set. Otherwise, when the
  last recorded pull is 7 or more days old (or there is none):
    GET /data/download
      ZIP        parsed in memory (src/privacy/drop.py, bounded), then recorded in ONE
                 transaction through migration 139's definer functions: the download, its
                 Email/Phone identifiers (batches of 5000), and the Removed file applied.
      no data    recorded as a pull with no file (evidence of the cadence).
      202        DROP is preparing the package: nothing recorded, the next daily run
                 asks again. Alerts only once the last pull is over 10 days old.
    Any failure sends an ops alert with the error class and HTTP status only (never an
    identifier, a hash or the key) and re-raises.

DROP requires a download at least every 45 days (30 from 2027-01-01, AB 883); the daily
retry with a 7-day spacing keeps well inside that. It does NOT match, withhold, erase or
report statuses (deliberately not built yet: tasks/todo-privacy-p9-drop.md). Note that
DROP answers 409 to a new download while the previous batch's statuses are unreported,
so once a ZIP has arrived, later pulls fail with 409 (and alert) until reporting exists.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.config import settings
from src.db.session import system_sync_session
from src.privacy.drop import (
    LIST_TYPES,
    DropClient,
    DropConflictError,
    DropError,
    DropFormatError,
    NoNewData,
    ParsedDownload,
    Preparing,
    ZipReady,
    parse_download,
)
from src.utils.logger import setup_logger

_logger = setup_logger("worker.privacy_drop")

_PULL_EVERY = timedelta(days=7)
_OVERDUE = timedelta(days=10)
_BATCH = 5000

_NOTICE = (
    "California's DROP regulations may require the data broker to give CalPrivacy "
    "written notice within 45 days when its automated connection to DROP fails. Counsel "
    "to confirm whether notice is owed for this failure; keep this alert as the record.")


def _chunks(pairs: list[tuple[str, str]]):
    for i in range(0, len(pairs), _BATCH):
        part = pairs[i:i + _BATCH]
        yield [p[0] for p in part], [p[1] for p in part]


def _record(db, file_name: str | None, parsed: ParsedDownload | None) -> dict:
    """Record one pull in the caller's transaction; returns counts only."""
    counts = parsed.list_counts if parsed else {}
    download_id = db.execute(text(
        "SELECT public.drop_begin_download(:f, CAST(:lc AS jsonb), CAST(:ic AS jsonb), "
        "CAST(:lf AS jsonb))"), {
        "f": file_name, "lc": json.dumps(counts),
        "ic": json.dumps(parsed.ignored_counts if parsed else {}),
        "lf": json.dumps(parsed.list_files if parsed else {})}).scalar()
    inserted = removed = 0
    if parsed:
        for list_type in LIST_TYPES:
            pairs = [(i, h) for t, i, h in parsed.items if t == list_type]
            for ids, hashes in _chunks(pairs):
                inserted += db.execute(text(
                    "SELECT public.drop_add_identifiers(:d, :t, CAST(:i AS text[]), "
                    "CAST(:h AS text[]))"), {"d": download_id, "t": list_type, "i": ids,
                                              "h": hashes}).scalar()
            gone = [(i, h) for t, i, h in parsed.removed if t == list_type]
            for ids, hashes in _chunks(gone):
                removed += db.execute(text(
                    "SELECT public.drop_mark_removed(:t, CAST(:i AS text[]), "
                    "CAST(:h AS text[]))"), {"t": list_type, "i": ids, "h": hashes}).scalar()
    return {"lists": counts, "ignored": parsed.ignored_counts if parsed else {},
            "inserted": inserted, "removed_applied": removed}


def _describe(exc: BaseException) -> str:
    """Safe text for an alert or log: our own DROP errors carry no data; anything else
    (a database error can echo its parameters) is reported by class name only."""
    if isinstance(exc, DropError):
        status = f", HTTP {exc.status}" if exc.status is not None else ""
        return f"{type(exc).__name__}{status}: {exc}"
    if isinstance(exc, DropFormatError):
        return f"DropFormatError: {exc}"
    return type(exc).__name__


def _alert(subject: str, detail: str, last: datetime | None) -> None:
    from src.workers.ops_alerts import send_ops_alert

    when = f"{last:%Y-%m-%d %H:%M} UTC" if last else "never"
    extra = ""
    if "DropConflictError" in detail:
        extra = ("\nDROP answers 409 while the previous download's statuses are "
                 "unreported; status reporting is not built yet (P9 foundation).")
    send_ops_alert(
        "drop_pull", datetime.now(UTC).strftime("%Y-%m-%d"), subject,
        f"The automated DROP list pull did not complete: {detail}.\n"
        f"Last recorded pull: {when}. No identifiers are included in this alert.{extra}\n"
        f"{_NOTICE}")


def pull_drop_lists() -> dict:
    if not (settings.DROP_ENABLED and settings.DROP_API_KEY):
        return {"status": "disabled"}
    with system_sync_session() as db:
        last = db.execute(text("SELECT public.drop_last_pull()")).scalar()
        db.rollback()
    now = datetime.now(UTC)
    if last is not None and now - last < _PULL_EVERY:
        return {"status": "not_due"}
    client = None
    try:
        client = DropClient(settings.DROP_API_KEY, settings.DROP_BASE_URL,
                            environment=settings.ENVIRONMENT)
        answer = client.download()
        if isinstance(answer, Preparing):
            _logger.info("DROP: the download package is being prepared; asking again tomorrow")
            if last is not None and now - last > _OVERDUE:
                _alert("DROP list pull overdue (package still preparing)",
                       "DROP answered 202 (preparing)", last)
            return {"status": "preparing", "retry_after": answer.retry_after}
        parsed = parse_download(answer.content) if isinstance(answer, ZipReady) else None
        with system_sync_session() as db:
            summary = _record(db, answer.file_name if parsed else None, parsed)
            db.commit()
    except Exception as exc:
        detail = _describe(exc)
        _logger.error("DROP list pull failed: %s", detail)
        kind = "rejected the request" if isinstance(exc, DropConflictError) else "failed"
        _alert(f"DROP list pull {kind} ({type(exc).__name__})", detail, last)
        raise
    finally:
        if client is not None:
            client.close()
    status = "no_new_data" if isinstance(answer, NoNewData) else "recorded"
    _logger.info("DROP pull %s: lists %s, ignored %s, %s new identifier(s), %s withdrawn",
                 status, summary["lists"], summary["ignored"], summary["inserted"],
                 summary["removed_applied"])
    return {"status": status, **summary}


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.privacy_drop.pull_drop_lists")
    def pull_drop_lists_task() -> dict:
        return pull_drop_lists()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.error("DROP pull task NOT registered: %s", str(exc)[:120])
