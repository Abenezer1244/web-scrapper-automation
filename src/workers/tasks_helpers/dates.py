"""Date-range resolution helpers, extracted from tasks.py.

Computes the (date_from, date_to) window a scrape job runs against from a
scraper's schedule config, and normalizes arbitrary date strings to the
MM/DD/YYYY county portals expect. Moved verbatim — behavior is byte-identical
to the originals in tasks.py.
"""

from datetime import datetime

from src.utils.logger import setup_logger

_logger = setup_logger("worker.task")

# Default lookback for tax-delinquent scrapes (ANY county). Tax delinquency is
# annual/multi-year, so the generic 90-day default would miss almost everyone;
# ~18 months is the fresh "last 1-1.5 years" motivated-seller target. Only the
# DEFAULT path is affected — an explicit rolling_30/7/custom/since_last_run wins.
_TAX_DELINQUENT_DEFAULT_DAYS = 548  # ~18 months


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


def _ordered_window(date_from: str, date_to: str) -> tuple[str, str]:
    """Never hand a scraper an inverted (date_from > date_to) window.

    An inverted range reaches county portals as garbage — empty results or a
    portal error — and is silently passed through downstream (the max-days trim
    only fires when a range is too LONG, never on negative days). Collapse an
    inverted window to a single day at date_to: the safe "nothing to scrape up to
    date_to" reading for a since_last_run same-day rerun, the starter 7-day-delay
    edge, or a backwards custom range that slipped past API validation (legacy
    rows / direct DB edits). Unparseable strings are returned untouched.
    """
    try:
        d0 = datetime.strptime(date_from, "%m/%d/%Y").date()
        d1 = datetime.strptime(date_to, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return date_from, date_to
    if d0 > d1:
        _logger.warning(
            "date range inverted (%s > %s) — collapsing to single day %s",
            date_from, date_to, date_to,
        )
        return date_to, date_to
    return date_from, date_to


def _resolve_date_range(schedule: dict, config_id: str | None = None, job_id: str | None = None, user_plan: str = "starter", record_type: str | None = None) -> tuple[str, str]:
    """Compute date_from and date_to from a scraper's schedule config.

    ``record_type`` selects the DEFAULT window: tax_delinquent defaults to ~18
    months (all counties), everything else to 90 days. Explicit modes win.
    """
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
            # Normalize to MM/DD/YYYY — frontend may send YYYY-MM-DD (ISO) — and
            # guard against a backwards custom range slipping through.
            return _ordered_window(_to_mmddyyyy(date_from), _to_mmddyyyy(date_to))
        # Fall through to rolling_90 if custom dates are missing

    if range_mode == "since_last_run":
        # Resume the day AFTER the GLOBAL max covered window end (date_to) across
        # ALL completed runs for this config — NOT the most-recently-FINISHED
        # job's date_to (Codex P1). A backfill/custom run can finish later while
        # covering an OLDER window, so keying on finish time (or even a bounded
        # "recent" scan) would rewind the start and re-scrape months. We compute
        # the max in Python (see the inner comment for why not Postgres to_date).
        # Fall back to the latest finish date, then 30 days, when no parseable
        # date_to exists. The +1 day avoids re-scraping the overlap day.
        from sqlalchemy import select

        from src.db.models import Job
        from src.db.session import SyncSessionLocal
        try:
            with SyncSessionLocal() as _db:
                _base = (
                    Job.scraper_config_id == config_id,
                    Job.status == "done",
                    Job.id != job_id,  # exclude current job
                )
                # Reduce in PYTHON, not via Postgres to_date: this DB runs strict
                # datetime mode, where to_date('11/31/2026') RAISES instead of
                # normalizing — one corrupt row would poison the whole aggregate
                # and silently drop us to the 30-day fallback (Codex). strptime
                # rejects an invalid calendar date cleanly (that row is skipped,
                # not fatal). date_to is one tiny column, so loading the config's
                # done-job set is cheap; no row-scan bound, so the max is GLOBAL.
                date_tos = _db.execute(
                    select(Job.date_to).where(*_base, Job.date_to.isnot(None))
                ).scalars().all()
                covered_to = None
                for d_to in date_tos:
                    try:
                        parsed = datetime.strptime(d_to, "%m/%d/%Y").date()
                    except (ValueError, TypeError):
                        continue  # shaped-but-invalid / junk — skip, don't poison
                    if covered_to is None or parsed > covered_to:
                        covered_to = parsed
                if covered_to is None:
                    # No parseable date_to on any run → latest finish date.
                    latest_finish = _db.execute(
                        select(Job.finished_at)
                        .where(*_base, Job.finished_at.isnot(None))
                        .order_by(Job.finished_at.desc())
                        .limit(1)
                    ).scalar()
                    covered_to = latest_finish.date() if latest_finish else None
                if covered_to is not None:
                    date_from = covered_to + timedelta(days=1)
                    if date_from > end_date:
                        # The last covered window already reaches end_date — the
                        # config is caught up, or a custom/backfill run covered
                        # into the future. There is no new forward day, so re-scan
                        # only the current end_date for late filings. Do it
                        # EXPLICITLY here rather than leaning on _ordered_window's
                        # inverted-range collapse below to produce the same window
                        # (Codex P2): the intent is auditable and a future refactor
                        # of _ordered_window can't silently break this path.
                        _logger.info(
                            "since_last_run: already covered through %s (>= end_date %s); "
                            "re-scanning end_date only for late filings",
                            covered_to, end_date,
                        )
                        date_from = end_date
                    else:
                        _logger.info(
                            "since_last_run: resuming from %s (day after max covered window end)",
                            date_from,
                        )
                else:
                    date_from = today - timedelta(days=30)
                    _logger.info(
                        "since_last_run: no usable previous window for config_id=%s, defaulting to 30 days",
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
        # Default path (no explicit/narrow mode). Tax-delinquent gets the ~18-month
        # window for ALL counties; other record types keep the 90-day default.
        default_days = _TAX_DELINQUENT_DEFAULT_DAYS if record_type == "tax_delinquent" else 90
        date_from = end_date - timedelta(days=default_days)

    return _ordered_window(date_from.strftime("%m/%d/%Y"), end_date.strftime("%m/%d/%Y"))
