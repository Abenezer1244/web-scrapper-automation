"""Dashboard analytics (Phase 3): one user-scoped summary endpoint.

Aggregates the leads (`results`) table over a calendar-day window in
ANALYTICS_TIMEZONE. Every query filters by current_user.id (RLS belt + filter
suspenders) and excludes duplicates. record_type/county come from a LEFT JOIN
through jobs -> scraper_configs (tenant-scoped on every hop); missing metadata
buckets as 'unknown'. No PII is read: phone/email presence is a NULL check on
the encrypted scalar columns. Queries run sequentially on the one async session
(the session is not concurrency-safe).
"""
from datetime import datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BeforeValidator
from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.middleware import rate_limit
from src.api.schemas import (
    AnalyticsSummary,
    CountyCount,
    RecordTypeCount,
    SkipTraceStats,
    TrendPoint,
)
from src.config.settings import settings
from src.db.models import Job, Result, ScraperConfig

router = APIRouter(prefix="/analytics", tags=["analytics"])

_TOP_COUNTIES = 8


def _coerce_window(value: object) -> object:
    """Coerce the ?window= query string so the Literal[30, 90] validates.

    Query values always arrive as strings, and pydantic v2 does NOT str->int
    coerce Literal members — so '?window=30' would 422 ("Input should be 30 or
    90") even though 30 is valid. Coerce digit strings to int here; leave any
    other input untouched so genuinely invalid values (e.g. '45', 'foo') still
    422 against the Literal.
    """
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


# 30/90-day window. BeforeValidator runs pre-Literal, preserving the 30|90
# enum in the OpenAPI schema (so the generated frontend types are unchanged).
WindowDays = Annotated[Literal[30, 90], BeforeValidator(_coerce_window), Query()]


def _with_meta_join(stmt: Select, uid: str) -> Select:
    """LEFT JOIN results -> jobs -> scraper_configs, tenant-scoped on every hop."""
    return (
        stmt.select_from(Result)
        .outerjoin(Job, and_(Job.id == Result.job_id, Job.user_id == uid))
        .outerjoin(
            ScraperConfig,
            and_(ScraperConfig.id == Job.scraper_config_id, ScraperConfig.user_id == uid),
        )
    )


@router.get("/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    request: Request,
    current_user: CurrentUser,
    window: WindowDays = 30,
    db: AsyncSession = Depends(get_rls_db),
) -> AnalyticsSummary:
    # Per-route throttle (4 aggregate queries per call). Matches the read-endpoint
    # pattern in jobs.py; the app rate-limits per-route, not via global middleware.
    await rate_limit(request, zone="general", identifier=current_user.id)
    uid = current_user.id
    tz_name = settings.ANALYTICS_TIMEZONE
    tz = ZoneInfo(tz_name)

    now_tz = datetime.now(tz)
    today = now_tz.date()
    start = today - timedelta(days=window - 1)
    # Redundant bare-column lower bound (1-day TZ slack) so the partial
    # (user_id, created_at) index range is usable; local_day does the exact filter.
    start_dt = datetime.combine(start, time.min, tzinfo=tz) - timedelta(days=1)
    local_day = func.date(func.timezone(tz_name, Result.created_at))

    base = (
        Result.user_id == uid,
        Result.is_duplicate.is_(False),
        Result.created_at >= start_dt,
        local_day >= start,
        # Upper bound at today's local day so a future-dated created_at (clock
        # skew) can't inflate by_record_type/county/skip_trace beyond the trend
        # (which only spans start..today) — keeps every aggregate's total consistent.
        local_day <= today,
    )

    # 1. trend — results only, grouped by local day.
    trend_rows = (
        await db.execute(
            select(local_day.label("day"), func.count().label("n"))
            .where(*base)
            .group_by("day")
        )
    ).all()
    counts_by_day = {r.day.isoformat(): r.n for r in trend_rows}
    trend = [
        TrendPoint(
            date=(d := (start + timedelta(days=i)).isoformat()),
            leads=counts_by_day.get(d, 0),
        )
        for i in range(window)
    ]

    # 2. by_record_type
    rt_col = func.coalesce(ScraperConfig.record_type, "unknown")
    rt_rows = (
        await db.execute(
            _with_meta_join(select(rt_col.label("rt"), func.count().label("n")), uid)
            .where(*base)
            .group_by("rt")
            .order_by(func.count().desc(), rt_col.asc())
        )
    ).all()
    by_record_type = [RecordTypeCount(record_type=r.rt, leads=r.n) for r in rt_rows]

    # 3. by_county — county+state key; top 8 then fold the tail into 'other'.
    # NULL county (missing metadata) is emitted separately and does NOT consume
    # a top-8 rank slot — it goes straight to the 'unknown' bucket.
    county_col = func.lower(ScraperConfig.county)
    state_col = func.upper(ScraperConfig.state)
    county_rows = (
        await db.execute(
            _with_meta_join(
                select(county_col.label("c"), state_col.label("s"), func.count().label("n")),
                uid,
            )
            .where(*base)
            .group_by("c", "s")
            .order_by(func.count().desc(), county_col.asc())
        )
    ).all()
    by_county: list[CountyCount] = []
    other = 0
    rank = 0
    for r in county_rows:
        if r.c is None:  # missing metadata (safety net; ~never with the NOT NULL FK chain)
            by_county.append(CountyCount(county="unknown", state=None, leads=r.n))
            continue
        if rank < _TOP_COUNTIES:
            by_county.append(CountyCount(county=r.c, state=r.s, leads=r.n))
            rank += 1
        else:
            other += r.n
    if other:
        by_county.append(CountyCount(county="other", state=None, leads=other))

    # 4. skip_trace — results only; presence = scalar primary IS NOT NULL.
    # Verified: phone/email absent == SQL NULL in the skip-trace writer.
    # tracerfy_ingest sets phone=None when no phones found (not an encrypted
    # empty string), so IS NOT NULL is a true presence test.
    # enriched = skip_trace_status='hit' (the real terminal status for a found
    # contact); 'done' is never written by the pipeline.
    st = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(Result.skip_trace_status == "hit").label("enriched"),
                func.count().filter(Result.phone.isnot(None)).label("phone"),
                func.count().filter(Result.email.isnot(None)).label("email"),
            ).where(*base)
        )
    ).one()
    total = st.total or 0
    skip_trace = SkipTraceStats(
        total=total,
        enriched=st.enriched or 0,
        phone_pct=round(100 * (st.phone or 0) / total) if total else 0,
        email_pct=round(100 * (st.email or 0) / total) if total else 0,
    )

    return AnalyticsSummary(
        window_days=window,
        timezone=tz_name,
        trend=trend,
        by_record_type=by_record_type,
        by_county=by_county,
        skip_trace=skip_trace,
    )
