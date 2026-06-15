"""Date-range resolution helpers, extracted from tasks.py.

Computes the (date_from, date_to) window a scrape job runs against from a
scraper's schedule config, and normalizes arbitrary date strings to the
MM/DD/YYYY county portals expect. Moved verbatim — behavior is byte-identical
to the originals in tasks.py.
"""

from datetime import datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.task")


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
        from sqlalchemy import select

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
