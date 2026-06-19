"""Dashboard analytics (Phase 3): one user-scoped summary endpoint.

Aggregates the leads (`results`) table over a calendar-day window in
ANALYTICS_TIMEZONE. Every query filters by current_user.id (RLS belt + filter
suspenders) and excludes duplicates. record_type/county come from a LEFT JOIN
through jobs -> scraper_configs (tenant-scoped on every hop); missing metadata
buckets as 'unknown'. No PII is read: phone/email presence is a NULL check on
the encrypted scalar columns. Queries run sequentially on the one async session
(the session is not concurrency-safe).
"""
from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
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


@router.get("/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    current_user: CurrentUser,
    window: Annotated[Literal[30, 90], Query()] = 30,
    db: AsyncSession = Depends(get_rls_db),
) -> AnalyticsSummary:
    uid = current_user.id
    tz_name = settings.ANALYTICS_TIMEZONE
    tz = ZoneInfo(tz_name)

    # Calendar-day window in the configured TZ: today + prior (window-1) days.
    today = datetime.now(tz).date()
    start = today - timedelta(days=window - 1)

    # created_at AT TIME ZONE tz, truncated to the local date.
    local_day = func.date(func.timezone(tz_name, Result.created_at))

    base = (
        Result.user_id == uid,
        Result.is_duplicate.is_(False),
        local_day >= start,
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
            select(rt_col.label("rt"), func.count().label("n"))
            .select_from(Result)
            .outerjoin(Job, and_(Job.id == Result.job_id, Job.user_id == uid))
            .outerjoin(
                ScraperConfig,
                and_(
                    ScraperConfig.id == Job.scraper_config_id,
                    ScraperConfig.user_id == uid,
                ),
            )
            .where(*base)
            .group_by("rt")
            .order_by(func.count().desc(), rt_col.asc())
        )
    ).all()
    by_record_type = [RecordTypeCount(record_type=r.rt, leads=r.n) for r in rt_rows]

    # 3. by_county — county+state key; top 8 then fold the tail into 'other'.
    county_col = func.lower(ScraperConfig.county)
    state_col = func.upper(ScraperConfig.state)
    county_rows = (
        await db.execute(
            select(county_col.label("c"), state_col.label("s"), func.count().label("n"))
            .select_from(Result)
            .outerjoin(Job, and_(Job.id == Result.job_id, Job.user_id == uid))
            .outerjoin(
                ScraperConfig,
                and_(
                    ScraperConfig.id == Job.scraper_config_id,
                    ScraperConfig.user_id == uid,
                ),
            )
            .where(*base)
            .group_by("c", "s")
            .order_by(func.count().desc(), county_col.asc())
        )
    ).all()
    by_county: list[CountyCount] = []
    other = 0
    for i, r in enumerate(county_rows):
        if r.c is None:  # missing metadata (safety net)
            by_county.append(CountyCount(county="unknown", state=None, leads=r.n))
        elif i < _TOP_COUNTIES:
            by_county.append(CountyCount(county=r.c, state=r.s, leads=r.n))
        else:
            other += r.n
    if other:
        by_county.append(CountyCount(county="other", state=None, leads=other))

    # 4. skip_trace — results only; presence = scalar primary IS NOT NULL.
    # Verified: phone/email absent == SQL NULL in the skip-trace writer.
    # tracerfy_ingest sets phone=None when no phones found (not an encrypted
    # empty string), so IS NOT NULL is a true presence test.
    st = (
        await db.execute(
            select(
                func.count().label("total"),
                func.count().filter(Result.skip_trace_status == "done").label("enriched"),
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
