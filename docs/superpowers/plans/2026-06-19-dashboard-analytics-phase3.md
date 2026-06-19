# Dashboard Analytics (Darkmatter Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real analytics layer to the dashboard — one RLS-scoped backend endpoint that aggregates the leads (`results`) table over a time window, rendered as four Recharts cards (leads trend, record-type mix, top counties, skip-trace rate).

**Architecture:** Backend-first across two repos. Phase 3a adds `GET /analytics/summary` to `web-scrapper-automation` (FastAPI + async SQLAlchemy + Postgres) with a partial index, merges + deploys, regenerates the OpenAPI contract. Phase 3b consumes the regenerated types in `bridgeleads-web` (Next.js 16 / React 19 / Tailwind v4) with shadcn chart + Recharts.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, Postgres (Supabase, RLS FORCE), Pydantic v2; Next.js, react-query v5, Recharts v3, shadcn `chart`.

**Spec:** `docs/superpowers/specs/2026-06-19-dashboard-analytics-phase3-design.md` (read it; this plan implements it).

## Global Constraints

- **Multi-tenancy:** every aggregate filters `Result.user_id == current_user.id`; every joined table carries its own `user_id` predicate. Endpoint uses `get_rls_db` (the `bridgeleads_app`, NOBYPASSRLS, RLS-context session) — NEVER `system_sync_session`.
- **No mock/dummy data** — every number traces to real rows. Real settings + real DB in tests (no `unittest.mock`); DB-backed tests are CI-arbitrated.
- **No PII leak:** count presence of the scalar primary `phone`/`email` via `IS NOT NULL`; never decrypt, never return phone/email values.
- **Window:** `30` or `90` only → anything else `422`. Bounded to the last N calendar days **including today** in `ANALYTICS_TIMEZONE` (default `America/Los_Angeles`).
- **Exclude duplicates:** every query filters `is_duplicate = false`.
- **Errors to clients:** reference id, never a raw DB error / stack trace.
- **Migrations:** run via `scripts/migrate.py` (advisory lock); big-table index = `CREATE INDEX CONCURRENTLY` in an `autocommit_block()`, never inside a txn. Latest revision is `067` → new is `068`.
- **Gates:** Phase 3a — Master Security §14 + Codex `review --base main`, any Crit/High = NO-GO. Phase 3b — `tsc --noEmit` + `eslint` clean + Codex review, any P1/High = NO-GO.

---

## File Structure

**Phase 3a — `web-scrapper-automation` (branch `feat/dashboard-analytics-phase3`, already created off `main`):**
- Create: `src/api/routes/analytics.py` — the endpoint + the 4 aggregate queries.
- Modify: `src/api/schemas.py` — add `AnalyticsSummary` + sub-models.
- Modify: `src/config/settings.py` + `.env.example` — add `ANALYTICS_TIMEZONE`.
- Modify: `main.py` — register `analytics_router`.
- Create: `alembic/versions/068_results_user_created_index.py` — partial index.
- Create: `tests/test_analytics.py` — endpoint tests.

**Phase 3b — `bridgeleads-web` (new branch `feat/dashboard-analytics-phase3` off `master`):**
- Modify: `package.json` — add `recharts`.
- Create: `components/ui/chart.tsx` — shadcn chart primitives.
- Modify: `lib/api.ts` — `getAnalyticsSummary` + `AnalyticsSummary` types.
- Modify: `lib/api-types.generated.ts` — regenerated from backend OpenAPI.
- Create: `app/(dashboard)/dashboard/_components/{LeadsTrendChart,RecordTypeMix,TopCountiesBars,SkipTraceRate}.tsx`.
- Modify: `app/(dashboard)/dashboard/page.tsx` — wire the analytics row.

---

# PHASE 3A — BACKEND

### Task 1: `ANALYTICS_TIMEZONE` setting

**Files:**
- Modify: `src/config/settings.py` (the `Settings(BaseSettings)` class)
- Modify: `.env.example`

**Interfaces:**
- Produces: `settings.ANALYTICS_TIMEZONE: str` (default `"America/Los_Angeles"`), consumed by Task 3.

- [ ] **Step 1: Add the setting field.** In `src/config/settings.py`, inside `class Settings(BaseSettings)`, near the other `str` config fields, add:

```python
    # Analytics (Phase 3): day-grouping timezone for the dashboard charts.
    # Postgres groups `date(created_at)` in the session TZ (UTC on Supabase),
    # which would split a Pacific user's day at the wrong boundary. The
    # analytics endpoint groups by created_at AT TIME ZONE this value. Must be
    # a valid IANA zone name.
    ANALYTICS_TIMEZONE: str = "America/Los_Angeles"
```

- [ ] **Step 2: Document it in `.env.example`.** Add:

```bash
# Timezone for dashboard analytics day-grouping (IANA name). Default Pacific.
ANALYTICS_TIMEZONE=America/Los_Angeles
```

- [ ] **Step 3: Verify it loads.** Run: `python -c "from src.config.settings import settings; print(settings.ANALYTICS_TIMEZONE)"`
Expected: `America/Los_Angeles`

- [ ] **Step 4: Commit.**

```bash
git add src/config/settings.py .env.example
git commit -m "feat(analytics): ANALYTICS_TIMEZONE setting for day-grouping"
```

---

### Task 2: Pydantic response schemas

**Files:**
- Modify: `src/api/schemas.py` (append near the other response models)

**Interfaces:**
- Produces: `AnalyticsSummary`, `TrendPoint`, `RecordTypeCount`, `CountyCount`, `SkipTraceStats` — consumed by Task 3 (`response_model=AnalyticsSummary`) and the frontend via OpenAPI.

- [ ] **Step 1: Add the schemas.** Append to `src/api/schemas.py`:

```python
class TrendPoint(BaseModel):
    date: str  # ISO date (YYYY-MM-DD) in ANALYTICS_TIMEZONE
    leads: int


class RecordTypeCount(BaseModel):
    record_type: str  # 'probate' | ... | 'unknown'
    leads: int


class CountyCount(BaseModel):
    county: str  # lowercased county, or 'other' / 'unknown'
    state: str | None  # uppercased 2-letter, None for 'other'/'unknown' buckets
    leads: int


class SkipTraceStats(BaseModel):
    total: int
    enriched: int  # skip_trace_status == 'done'
    phone_pct: int  # 0-100, share of total with a primary phone
    email_pct: int  # 0-100, share of total with a primary email


class AnalyticsSummary(BaseModel):
    window_days: int
    timezone: str
    trend: list[TrendPoint]  # dense, zero-filled, today inclusive
    by_record_type: list[RecordTypeCount]
    by_county: list[CountyCount]
    skip_trace: SkipTraceStats
```

- [ ] **Step 2: Verify import.** Run: `python -c "from src.api.schemas import AnalyticsSummary; print(AnalyticsSummary.model_fields.keys())"`
Expected: prints the 5 field names.

- [ ] **Step 3: Commit.**

```bash
git add src/api/schemas.py
git commit -m "feat(analytics): AnalyticsSummary response schemas"
```

---

### Task 3: The `/analytics/summary` endpoint

> **Pre-task verification (spec §9, do FIRST — they shape the code below):**
> 1. `Result.job_id` and `Job.scraper_config_id` are both `NOT NULL` + `ondelete=CASCADE` (confirmed in `src/db/models.py:443-448`, `492+`) → orphans are impossible today, so the LEFT JOIN's `unknown` bucket is a safety net, not an expected value. Keep the LEFT JOIN (cheap, future-proof).
> 2. Confirm the skip-trace pipeline leaves `Result.phone`/`email` as SQL `NULL` (not `""`-as-ciphertext) when there's no result — grep the skip-trace writer in `workers/`/`src/`. If it can write an encrypted empty string, switch the presence test to also exclude that. Record the finding in the commit message.
> 3. `bridgeleads_app` is `NOBYPASSRLS` (H1) — the isolation test asserts cross-tenant rows never appear.

**Files:**
- Create: `src/api/routes/analytics.py`
- Modify: `main.py` (register router)
- Test: `tests/test_analytics.py`

**Interfaces:**
- Consumes: `CurrentUser`, `get_rls_db` (`src/api/auth`, `src/api/deps`); `Result`, `Job`, `ScraperConfig` (`src/db/models`); `AnalyticsSummary` & sub-models (Task 2); `settings.ANALYTICS_TIMEZONE` (Task 1).
- Produces: `GET /analytics/summary?window=30|90 -> AnalyticsSummary`; `analytics_router` imported by `main.py`.

- [ ] **Step 1: Write the failing isolation + shape test.** Create `tests/test_analytics.py`. Use the repo's existing async test client + real-DB fixtures (mirror `tests/test_notifications.py` for fixture names — user factory, auth headers, db session). Core tests:

```python
import pytest

# Mirrors the auth/client/db fixtures used in tests/test_notifications.py.

@pytest.mark.asyncio
async def test_summary_requires_auth(client):
    r = await client.get("/analytics/summary")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_summary_rejects_bad_window(client, auth_headers):
    r = await client.get("/analytics/summary?window=7", headers=auth_headers)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_summary_empty_account_all_zeros(client, auth_headers):
    r = await client.get("/analytics/summary?window=30", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 30
    assert body["timezone"]  # non-empty
    assert len(body["trend"]) == 30  # dense, zero-filled, incl today
    assert all(p["leads"] == 0 for p in body["trend"])
    assert body["by_record_type"] == []
    assert body["by_county"] == []
    assert body["skip_trace"] == {"total": 0, "enriched": 0, "phone_pct": 0, "email_pct": 0}


@pytest.mark.asyncio
async def test_summary_tenant_isolation(client, auth_headers, other_users_lead):
    # other_users_lead seeds a non-dup, in-window result for a DIFFERENT user.
    r = await client.get("/analytics/summary?window=30", headers=auth_headers)
    body = r.json()
    assert sum(p["leads"] for p in body["trend"]) == 0  # never see the other tenant
    assert body["skip_trace"]["total"] == 0


@pytest.mark.asyncio
async def test_summary_counts_own_leads(client, auth_headers, seed_my_leads):
    # seed_my_leads: 3 non-dup probate results in King WA today + 1 is_duplicate=true.
    r = await client.get("/analytics/summary?window=30", headers=auth_headers)
    body = r.json()
    assert sum(p["leads"] for p in body["trend"]) == 3  # duplicate excluded
    rt = {x["record_type"]: x["leads"] for x in body["by_record_type"]}
    assert rt.get("probate") == 3
    counties = {(c["county"], c["state"]): c["leads"] for c in body["by_county"]}
    assert counties.get(("king", "WA")) == 3
```

> If the existing fixtures differ, adapt names but keep the assertions. `seed_my_leads`/`other_users_lead`/`auth_headers` create REAL rows via the models (no mocks).

- [ ] **Step 2: Run to verify it fails.** Run: `pytest tests/test_analytics.py -v`
Expected: FAIL / errors (route + fixtures not defined yet).

- [ ] **Step 3: Write the endpoint.** Create `src/api/routes/analytics.py`:

```python
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

    # LEFT JOIN to scraper metadata, tenant-scoped on every hop.
    meta_join = (
        select(Result)
        .outerjoin(Job, and_(Job.id == Result.job_id, Job.user_id == uid))
        .outerjoin(
            ScraperConfig,
            and_(
                ScraperConfig.id == Job.scraper_config_id,
                ScraperConfig.user_id == uid,
            ),
        )
        .where(*base)
    ).subquery()  # not used directly; pattern shown — build per-aggregate below

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
```

> Remove the unused `meta_join` subquery block before committing — it documents the join shape but each aggregate builds its own; keep the code clean (ruff will flag the unused name).

- [ ] **Step 4: Register the router.** In `main.py`, add the import alongside the others and register after `notifications_router`:

```python
from src.api.routes.analytics import router as analytics_router
# ...
app.include_router(analytics_router)
```

- [ ] **Step 5: Run the tests.** Run: `pytest tests/test_analytics.py -v`
Expected: PASS (all). If fixtures need seeding helpers, add them in the test file using real model inserts.

- [ ] **Step 6: Lint.** Run: `ruff check src/api/routes/analytics.py src/api/schemas.py tests/test_analytics.py`
Expected: clean (fix any unused imports / the `meta_join` removal).

- [ ] **Step 7: Commit.**

```bash
git add src/api/routes/analytics.py main.py tests/test_analytics.py
git commit -m "feat(analytics): GET /analytics/summary — user-scoped lead aggregates

trend/skip_trace read results only; record_type/county via tenant-scoped
LEFT JOIN to scraper_configs (unknown bucket safety net). TZ-day grouping,
duplicates excluded, scalar-primary phone/email presence (no decrypt).
Verified: phone absent == SQL NULL in the skip-trace writer."
```

---

### Task 4: Partial index migration `068`

**Files:**
- Create: `alembic/versions/068_results_user_created_index.py`

**Interfaces:**
- Produces: partial index `ix_results_user_created` on `results(user_id, created_at) WHERE is_duplicate = false`.

- [ ] **Step 1: Write the migration** (mirrors `033`'s CONCURRENTLY + invalid-index preflight). Create `alembic/versions/068_results_user_created_index.py`:

```python
"""Analytics: partial index results(user_id, created_at) (068).

Phase 3 dashboard analytics windows by (user_id, created_at) over non-duplicate
leads. The existing ix_results_job_user_dup_created leads with job_id, so it
can't serve a (user_id, created_at) range scan. Partial WHERE is_duplicate=false
matches the endpoint's predicate exactly. CONCURRENTLY (no write lock on the
large results table) requires autocommit (no txn). Idempotent + invalid-index
preflight, per the 033 pattern.

Revision ID: 068
Revises: 067
Create Date: 2026-06-19
"""
from alembic import op
from sqlalchemy import text

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None

_INDEX = "ix_results_user_created"
_CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
    "ON results (user_id, created_at) WHERE is_duplicate = false"
)
_INVALID_CHECK = text(
    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "WHERE c.relname = :name AND NOT i.indisvalid"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        if conn.execute(_INVALID_CHECK, {"name": _INDEX}).first():
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
        conn.execute(text(_CREATE))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.get_bind().execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
```

- [ ] **Step 2: Sanity-check it parses + heads.** Run: `python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print([h for h in s.get_heads()])"`
Expected: a single head `068` (no multiple-heads branch). Do NOT apply to prod here — CI/operator applies.

- [ ] **Step 3: Lint.** Run: `ruff check alembic/versions/068_results_user_created_index.py`
Expected: clean.

- [ ] **Step 4: Commit.**

```bash
git add alembic/versions/068_results_user_created_index.py
git commit -m "feat(analytics): partial index results(user_id, created_at)

CONCURRENTLY, WHERE is_duplicate=false — matches /analytics/summary."
```

---

### 🚦 PHASE 3A GATE (do NOT start 3b until all green)

- [ ] Run the full suite locally where possible: `pytest tests/test_analytics.py -v` + `ruff check .`
- [ ] **Master Security §14** review (multi-tenancy isolation, no PII leak, input validation, error refs). Two consecutive clean passes.
- [ ] **Codex** `codex review --base main -c mcp_servers={} < /dev/null` (pipe through `grep -a`). Any Crit/High = NO-GO; fix + re-review.
- [ ] Open PR vs `main`; ensure CI green (the `068` migration applies on Railway boot — confirm no multiple-heads).
- [ ] Merge → Railway api+worker deploy SUCCESS → `/health` 200 → one live `GET /analytics/summary?window=30` on a real account returns 200 with sane numbers (proof-of-work).
- [ ] **Regenerate the backend OpenAPI** (`schema/openapi.json`) in the pinned `.venv-schema` and commit — the frontend type gen + CI drift gate depend on it. (Without this, 3b's `gen:api-types` won't see the new endpoint.)

---

# PHASE 3B — FRONTEND (`bridgeleads-web`)

> Start only after 3a is merged + deployed + OpenAPI regenerated. Branch: `git switch -c feat/dashboard-analytics-phase3 origin/master`.

### Task 5: Data layer — deps, types, API function

**Files:**
- Modify: `package.json` (add `recharts`)
- Create: `components/ui/chart.tsx` (shadcn chart)
- Modify: `lib/api.ts`
- Modify: `lib/api-types.generated.ts` (regenerated)

**Interfaces:**
- Produces: `getAnalyticsSummary(window: 30 | 90): Promise<AnalyticsSummary>` and the `AnalyticsSummary` TS type — consumed by Tasks 6-8.

- [ ] **Step 1: Add Recharts + shadcn chart.** Run: `npx shadcn@latest add chart` (creates `components/ui/chart.tsx` and adds `recharts` to `package.json`). If the registry is unavailable, `npm i recharts` and hand-add the shadcn `chart.tsx` from the pinned shadcn source.

- [ ] **Step 2: SBOM/audit the new dep.** Run: `npm audit --omit=dev` — expect 0 new advisories from `recharts`. Record the result.

- [ ] **Step 3: Regenerate API types.** Run: `npm run gen:api-types`
Expected: `lib/api-types.generated.ts` now includes `/analytics/summary` + `AnalyticsSummary`. (If it doesn't, 3a's OpenAPI wasn't regenerated — go back.)

- [ ] **Step 4: Add the API function + types.** In `lib/api.ts`, near `getUsage`:

```typescript
// ─── Analytics (Phase 3) ────────────────────────────────────────────────────
export interface TrendPoint { date: string; leads: number }
export interface RecordTypeCount { record_type: string; leads: number }
export interface CountyCount { county: string; state: string | null; leads: number }
export interface SkipTraceStats { total: number; enriched: number; phone_pct: number; email_pct: number }
export interface AnalyticsSummary {
  window_days: number;
  timezone: string;
  trend: TrendPoint[];
  by_record_type: RecordTypeCount[];
  by_county: CountyCount[];
  skip_trace: SkipTraceStats;
}

export async function getAnalyticsSummary(window: 30 | 90): Promise<AnalyticsSummary> {
  return apiFetch<AnalyticsSummary>(`/analytics/summary?window=${window}`);
}
```

- [ ] **Step 5: Typecheck.** Run: `npx tsc --noEmit`
Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add package.json package-lock.json components/ui/chart.tsx lib/api.ts lib/api-types.generated.ts
git commit -m "feat(analytics): recharts + shadcn chart + getAnalyticsSummary"
```

---

### Task 6: `LeadsTrendChart` card (area + 30/90 toggle)

**Files:**
- Create: `app/(dashboard)/dashboard/_components/LeadsTrendChart.tsx`

**Interfaces:**
- Consumes: `getAnalyticsSummary`, `AnalyticsSummary` (Task 5).
- Produces: `<LeadsTrendChart />` — owns its own `["analytics", window]` query + the window toggle.

- [ ] **Step 1: Write the component.** Create the file:

```tsx
"use client";

import { useState } from "react";
import { useSession } from "next-auth/react";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts";
import { motion } from "framer-motion";
import { getAnalyticsSummary } from "@/lib/api";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/error-state";
import { fadeUp } from "../_lib";

const WINDOWS = [30, 90] as const;

export function LeadsTrendChart() {
  const { data: session } = useSession();
  const [window, setWindow] = useState<30 | 90>(30);
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["analytics", window],
    queryFn: () => getAnalyticsSummary(window),
    enabled: !!session,
  });

  const total = data?.trend.reduce((s, p) => s + p.leads, 0) ?? 0;

  return (
    <motion.div
      custom={0}
      variants={fadeUp}
      initial="hidden"
      animate="visible"
      className="lg:col-span-2 rounded-2xl border p-5 shadow-bevel"
      style={{ backgroundColor: "var(--card)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
            Leads over time
          </h2>
          <p className="text-xs" style={{ color: "var(--color-text-secondary)" }}>
            {total.toLocaleString()} leads · last {window} days
          </p>
        </div>
        <div className="flex gap-1 rounded-lg p-0.5" style={{ backgroundColor: "var(--muted)" }}>
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setWindow(w)}
              className="px-3 py-1 rounded-md text-xs font-medium transition-colors"
              style={{
                backgroundColor: window === w ? "var(--card)" : "transparent",
                color: window === w ? "var(--color-teal)" : "var(--color-text-secondary)",
              }}
            >
              {w}d
            </button>
          ))}
        </div>
      </div>

      {isError ? (
        <ErrorState title="Couldn't load analytics" onRetry={() => refetch()} />
      ) : isLoading || !data ? (
        <Skeleton variant="card" className="h-56" />
      ) : total === 0 ? (
        <div className="h-56 flex items-center justify-center text-sm" style={{ color: "var(--color-text-secondary)" }}>
          No leads yet — run a scraper to see your pipeline trend.
        </div>
      ) : (
        <ChartContainer config={{ leads: { label: "Leads", color: "var(--color-teal)" } }} className="h-56 w-full">
          <AreaChart data={data.trend} margin={{ left: -16, right: 8, top: 8 }}>
            <defs>
              <linearGradient id="leadsFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-teal)" stopOpacity={0.35} />
                <stop offset="100%" stopColor="var(--color-teal)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis dataKey="date" tickLine={false} axisLine={false} minTickGap={32}
                   tickFormatter={(d: string) => d.slice(5)} fontSize={11} />
            <YAxis tickLine={false} axisLine={false} width={32} fontSize={11} allowDecimals={false} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Area dataKey="leads" type="monotone" stroke="var(--color-teal)" strokeWidth={2} fill="url(#leadsFill)" />
          </AreaChart>
        </ChartContainer>
      )}
    </motion.div>
  );
}
```

- [ ] **Step 2: Typecheck + lint.** Run: `npx tsc --noEmit && npm run lint`
Expected: clean. (If `ChartTooltipContent` prop types differ in the installed shadcn chart, adjust to its actual export.)

- [ ] **Step 3: Commit.**

```bash
git add "app/(dashboard)/dashboard/_components/LeadsTrendChart.tsx"
git commit -m "feat(analytics): LeadsTrendChart area chart + 30/90 toggle"
```

---

### Task 7: `RecordTypeMix`, `TopCountiesBars`, `SkipTraceRate` cards

**Files:**
- Create: `RecordTypeMix.tsx`, `TopCountiesBars.tsx`, `SkipTraceRate.tsx` (same `_components/` dir)

**Interfaces:**
- Consumes: the shared `["analytics", 30]` query (read from cache; these cards use the default 30-day window — only the trend toggles). Each takes its slice as a prop from the page OR runs `useQuery(["analytics", 30])` itself (cache-deduped). **Decision: each card runs `useQuery(["analytics", 30])`** — react-query dedupes by key, so no prop-drilling; the trend card's toggle is independent (its own key when 90).
- Produces: three presentational cards.

> All three share the same query + four-state skeleton, reading the default-30 window from cache (`["analytics", 30]`, deduped with the trend card when it is on 30). Each copies the `motion.div` card shell from Task 6 (do NOT abstract a shared card yet — YAGNI; revisit if a 5th card appears).

- [ ] **Step 1: `RecordTypeMix.tsx`** — donut, slice color via `recordTypeTone`:

```tsx
"use client";

import { useSession } from "next-auth/react";
import { useQuery } from "@tanstack/react-query";
import { Cell, Pie, PieChart } from "recharts";
import { motion } from "framer-motion";
import { getAnalyticsSummary } from "@/lib/api";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/error-state";
import { recordTypeTone, capitalize } from "@/lib/utils";
import { fadeUp } from "../_lib";

export function RecordTypeMix() {
  const { data: session } = useSession();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["analytics", 30],
    queryFn: () => getAnalyticsSummary(30),
    enabled: !!session,
  });
  const slices = data?.by_record_type ?? [];

  return (
    <motion.div custom={1} variants={fadeUp} initial="hidden" animate="visible"
      className="rounded-2xl border p-5 shadow-bevel"
      style={{ backgroundColor: "var(--card)", borderColor: "var(--border)" }}>
      <h2 className="text-base font-semibold mb-4" style={{ color: "var(--color-text-primary)" }}>Lead mix</h2>
      {isError ? (
        <ErrorState title="Couldn't load lead mix" onRetry={() => refetch()} />
      ) : isLoading || !data ? (
        <Skeleton variant="card" className="h-48" />
      ) : slices.length === 0 ? (
        <div className="h-48 flex items-center justify-center text-sm" style={{ color: "var(--color-text-secondary)" }}>
          No leads yet.
        </div>
      ) : (
        <div className="flex items-center gap-4">
          <ChartContainer config={{}} className="h-40 w-40 shrink-0">
            <PieChart>
              <ChartTooltip content={<ChartTooltipContent nameKey="record_type" />} />
              <Pie data={slices} dataKey="leads" nameKey="record_type" innerRadius={42} outerRadius={64} strokeWidth={0}>
                {slices.map((s) => (
                  <Cell key={s.record_type} fill={recordTypeTone(s.record_type)} />
                ))}
              </Pie>
            </PieChart>
          </ChartContainer>
          <ul className="flex-1 space-y-1.5">
            {slices.map((s) => (
              <li key={s.record_type} className="flex items-center gap-2 text-xs" style={{ color: "var(--color-text-secondary)" }}>
                <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: recordTypeTone(s.record_type) }} />
                <span className="flex-1">{capitalize(s.record_type)}</span>
                <span className="font-mono tabular-nums" style={{ color: "var(--color-text-primary)" }}>{s.leads.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </motion.div>
  );
}
```

- [ ] **Step 2: `TopCountiesBars.tsx`** — horizontal bars:

```tsx
"use client";

import { useSession } from "next-auth/react";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, XAxis, YAxis } from "recharts";
import { motion } from "framer-motion";
import { getAnalyticsSummary, type CountyCount } from "@/lib/api";
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/error-state";
import { capitalize } from "@/lib/utils";
import { fadeUp } from "../_lib";

const label = (c: CountyCount) => `${capitalize(c.county)}${c.state ? ` ${c.state}` : ""}`;

export function TopCountiesBars() {
  const { data: session } = useSession();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["analytics", 30],
    queryFn: () => getAnalyticsSummary(30),
    enabled: !!session,
  });
  const rows = (data?.by_county ?? []).map((c) => ({ ...c, label: label(c) }));

  return (
    <motion.div custom={2} variants={fadeUp} initial="hidden" animate="visible"
      className="lg:col-span-2 rounded-2xl border p-5 shadow-bevel"
      style={{ backgroundColor: "var(--card)", borderColor: "var(--border)" }}>
      <h2 className="text-base font-semibold mb-4" style={{ color: "var(--color-text-primary)" }}>Top counties</h2>
      {isError ? (
        <ErrorState title="Couldn't load counties" onRetry={() => refetch()} />
      ) : isLoading || !data ? (
        <Skeleton variant="card" className="h-48" />
      ) : rows.length === 0 ? (
        <div className="h-48 flex items-center justify-center text-sm" style={{ color: "var(--color-text-secondary)" }}>
          No leads yet.
        </div>
      ) : (
        <ChartContainer config={{ leads: { label: "Leads", color: "var(--color-teal)" } }} className="h-48 w-full">
          <BarChart data={rows} layout="vertical" margin={{ left: 8, right: 16 }}>
            <XAxis type="number" hide allowDecimals={false} />
            <YAxis type="category" dataKey="label" width={96} tickLine={false} axisLine={false} fontSize={11} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Bar dataKey="leads" fill="var(--color-teal)" radius={4} />
          </BarChart>
        </ChartContainer>
      )}
    </motion.div>
  );
}
```

- [ ] **Step 3: `SkipTraceRate.tsx`** — stat + phone/email mini-bars:

```tsx
"use client";

import { useSession } from "next-auth/react";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { getAnalyticsSummary } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/error-state";
import { fadeUp } from "../_lib";

function MiniBar({ label, pct }: { label: string; pct: number }) {
  return (
    <div>
      <div className="flex justify-between text-xs mb-1" style={{ color: "var(--color-text-secondary)" }}>
        <span>{label}</span><span className="font-mono tabular-nums">{pct}%</span>
      </div>
      <div className="h-1.5 rounded-full" style={{ backgroundColor: "var(--muted)" }}>
        <div className="h-1.5 rounded-full" style={{ width: `${pct}%`, backgroundColor: "var(--color-teal)" }} />
      </div>
    </div>
  );
}

export function SkipTraceRate() {
  const { data: session } = useSession();
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["analytics", 30],
    queryFn: () => getAnalyticsSummary(30),
    enabled: !!session,
  });
  const st = data?.skip_trace;
  const rate = st && st.total > 0 ? Math.round((100 * st.enriched) / st.total) : 0;

  return (
    <motion.div custom={1} variants={fadeUp} initial="hidden" animate="visible"
      className="rounded-2xl border p-5 shadow-bevel"
      style={{ backgroundColor: "var(--card)", borderColor: "var(--border)" }}>
      <h2 className="text-base font-semibold mb-4" style={{ color: "var(--color-text-primary)" }}>Skip-trace hit rate</h2>
      {isError ? (
        <ErrorState title="Couldn't load skip-trace stats" onRetry={() => refetch()} />
      ) : isLoading || !st ? (
        <Skeleton variant="card" className="h-40" />
      ) : st.total === 0 ? (
        <div className="h-40 flex items-center justify-center text-sm text-center" style={{ color: "var(--color-text-secondary)" }}>
          No leads yet — enrichment stats appear after your first scrape.
        </div>
      ) : st.enriched === 0 ? (
        <div className="h-40 flex items-center justify-center text-sm text-center" style={{ color: "var(--color-text-secondary)" }}>
          Skip-trace isn&apos;t enabled on these leads. Turn it on to get phone &amp; email.
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <div className="text-4xl font-bold" style={{ color: "var(--color-teal)" }}>{rate}%</div>
            <p className="text-xs" style={{ color: "var(--color-text-secondary)" }}>
              {st.enriched.toLocaleString()} of {st.total.toLocaleString()} leads enriched
            </p>
          </div>
          <MiniBar label="Phone" pct={st.phone_pct} />
          <MiniBar label="Email" pct={st.email_pct} />
        </div>
      )}
    </motion.div>
  );
}
```

- [ ] **Step 4: Typecheck + lint.** Run: `npx tsc --noEmit && npm run lint`
Expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add "app/(dashboard)/dashboard/_components/RecordTypeMix.tsx" "app/(dashboard)/dashboard/_components/TopCountiesBars.tsx" "app/(dashboard)/dashboard/_components/SkipTraceRate.tsx"
git commit -m "feat(analytics): record-type mix, top-counties bars, skip-trace rate cards"
```

---

### Task 8: Wire the analytics row + ship

**Files:**
- Modify: `app/(dashboard)/dashboard/page.tsx`

**Interfaces:**
- Consumes: the four card components (Tasks 6-7).

- [ ] **Step 1: Add the row.** In `dashboard/page.tsx`, import the four cards and insert an Analytics section **between** `<StatCards .../>` and the existing bento `<div className="grid ...">`:

```tsx
import { LeadsTrendChart } from "./_components/LeadsTrendChart";
import { RecordTypeMix } from "./_components/RecordTypeMix";
import { TopCountiesBars } from "./_components/TopCountiesBars";
import { SkipTraceRate } from "./_components/SkipTraceRate";
// ...
{/* ─── Analytics row ─────────────────────────────────────────────── */}
<div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
  <LeadsTrendChart />            {/* spans 2 cols via its own lg:col-span-2 */}
  <SkipTraceRate />
  <RecordTypeMix />
  <TopCountiesBars />
</div>
```

- [ ] **Step 2: Typecheck + lint.** Run: `npx tsc --noEmit && npm run lint`
Expected: clean.

- [ ] **Step 3: Visual verify (headless — headed browse is broken on this box).** Start/confirm dev server on :3000, log in (`admin@bridgeleads.io` / `BridgeLeads2026!`), screenshot `/dashboard`. Confirm: trend area renders + toggles 30/90, donut colors match record types, bars show counties, skip-trace stat renders; all four show loading→data and a sane empty state on a fresh account.

- [ ] **Step 4: Commit.**

```bash
git add "app/(dashboard)/dashboard/page.tsx"
git commit -m "feat(analytics): wire the analytics row into the dashboard"
```

---

### 🚦 PHASE 3B GATE

- [ ] `npx tsc --noEmit` + `npm run lint` clean.
- [ ] **Codex** `codex review --base origin/master -c mcp_servers={} < /dev/null` (grep -a). Any P1/High = NO-GO.
- [ ] Visual proof (screenshots of the four charts with real data).
- [ ] PR vs `master` green (incl. the api-types drift gate — already regenerated in Task 5).
- [ ] On user's go: merge → one Vercel prod deploy → verify prod green (`bridgeleads.io`/`app.bridgeleads.io` 200, dashboard charts render).

---

## Notes for the implementer

- **TDD discipline:** for the backend endpoint, the isolation + empty-account tests are the spec. Write them first, watch them fail, then implement.
- **DRY caution:** the four frontend cards share a card shell — copy it (Task 7 note); only extract a shared `<AnalyticsCard>` if a 5th card appears (YAGNI).
- **The `unknown`/`other` buckets** are real response values — the frontend must render them (don't filter them out); `unknown` should ~never appear given the FK constraints, `other` appears once a tenant has >8 counties.
- **Do not run `migrate.py`/alembic/DB tests against prod locally** — CI arbitrates; the operator applies `068` on deploy.
