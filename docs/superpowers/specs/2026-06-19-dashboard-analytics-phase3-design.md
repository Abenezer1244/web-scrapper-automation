# Dashboard Analytics — Darkmatter Phase 3 (design)

**Date:** 2026-06-19
**Status:** approved (brainstorm) — pending Codex consult + user spec review
**Scope:** cross-repo. Backend `web-scrapper-automation` (new analytics endpoint + index migration)
then frontend `bridgeleads-web` (four chart cards). Backend-first, per the OpenAPI type-contract rule.
**Continues:** [[project_darkmatter_ui_redesign_2026_06_17]] — Phases 0→2b shipped to prod 2026-06-19
(`cf0d182`). This is Phase 3 (dashboard widgets + charts). Phases 4 (marketing FX), 5 (auth polish),
6 (sub-pages) follow separately.

---

## 1. Goal

Add a real analytics layer to the dashboard for a real-estate wholesaler: *is my lead pipeline
growing, what's the lead mix, where do leads come from, and are they getting enriched.* Four charts,
driven by one new RLS-scoped backend endpoint that aggregates the `results` (leads) table over a
bounded time window. **No mock data** (CLAUDE.md non-negotiable) — every number traces to real rows.

## 2. Data reality (constraints that shaped this)

- `results` (leads) has: `user_id` (indexed), `created_at`, `skip_trace_status`, `phone`/`email`
  (encrypted), `phones`/`emails`, `is_duplicate`, `job_id` (indexed). It does **not** carry
  `record_type` or `county` — those live on `scraper_configs`.
- To get record-type / county per lead: join `results → jobs (job_id) → scraper_configs
  (scraper_config_id)`, reading `scraper_configs.record_type` / `.county`.
- The existing `GET /jobs` is capped at the last 100 jobs and is **not** a usable history source —
  hence a dedicated analytics endpoint (the user explicitly chose real history over deriving from the
  100-job list).
- `created_at` = when the lead landed in the product → the right axis for "pipeline growth" (vs
  `date_recorded_parsed` = county filing date). The trend uses `created_at`.
- Today only `results(user_id)` is indexed (plus `job_id`, `dedup_hash`, `raw_html_hash`). A
  windowed aggregate needs a composite **`results(user_id, created_at)`** index.

## 3. Architecture

### 3.1 Backend — one endpoint (Approach A, chosen)

`GET /analytics/summary?window=30` (allowed: `30`, `90`; default `30`). Auth = standard JWT/API-key
dependency. **Mandatory `user_id` query filter on every aggregate** (RLS belt + filter suspenders).
All aggregates exclude duplicates (`is_duplicate = false`) and are bounded to
`created_at >= now() - window days`.

Response (`AnalyticsSummary` Pydantic schema):

```jsonc
{
  "window_days": 30,
  "trend":          [{ "date": "2026-06-01", "leads": 124 }, ...],   // dense: zero-filled per day
  "by_record_type": [{ "record_type": "probate", "leads": 980 }, ...],
  "by_county":      [{ "county": "king", "leads": 4210 }, ...],       // top 8, rest folded into "other"
  "skip_trace":     { "total": 2600, "enriched": 1840, "phone_pct": 64, "email_pct": 52 }
}
```

Four aggregates, each its own `SELECT`:

1. **trend** — `results` group by `date(created_at)`, count. Zero-fill missing days **in Python** so
   the chart x-axis is continuous (DB returns only days with rows).
2. **by_record_type** — `results JOIN jobs JOIN scraper_configs` group by `record_type`, count.
3. **by_county** — same join, group by `lower(county)`, count, order desc, top 8; remainder summed
   into a single `{"county": "other", ...}` bucket **in Python**.
4. **skip_trace** — `results` filtered to the window: `total` = all non-dup leads; `enriched` =
   `skip_trace_status='done'`; `phone_pct` = leads with a non-null `phone`/`phones` over total;
   `email_pct` likewise. Percentages computed in Python (guard divide-by-zero → 0).

New file `src/api/routes/analytics.py`; router registered in `main.py`. Schema in
`src/api/schemas.py`. The `phone`/`email` columns are `EncryptedString`; **count presence via the
plaintext-safe path** — use `phone IS NOT NULL` at the SQL level (the ciphertext is still non-null) /
or the existing `phones` JSON length; do **not** decrypt in the aggregate (no PII read, just presence).

### 3.2 Index migration

New Alembic migration: composite index `ix_results_user_created` on `results(user_id, created_at)`.
Created **`CONCURRENTLY`** with `statement_timeout=0` (the `results` table is large — see the landmine
memo [[incident_backfill_blocks_migration]]: never block on a big-table index). Run via
`scripts/migrate.py` (advisory lock). RLS: `results` is already an enforced/FORCE table from H1 — no
new policy needed; the endpoint runs on `bridgeleads_app` and the `user_id` filter + existing
`results` policy cover it. **4-step RLS drift checklist N/A** (no new table).

### 3.3 Frontend — four chart cards

- Deps: add `recharts` (v3) + shadcn `components/ui/chart.tsx` (`ChartContainer`/`ChartTooltip`).
  SBOM/`npm audit` check before commit (vetted-registry rule).
- `lib/api.ts`: `getAnalyticsSummary(window: 30 | 90)`. Regenerate `lib/api-types.generated.ts` from
  the backend OpenAPI **after** the backend merges (the CI type-drift gate — see the 06-19 ship).
- New `app/(dashboard)/dashboard/_components/`:
  - `LeadsTrendChart.tsx` — Recharts `AreaChart`, teal (`--brand-teal`/`--chart-1`) fill gradient,
    30/90 toggle (segmented control; refetches with the new `window`).
  - `RecordTypeMix.tsx` — donut, per-slice color via existing `recordTypeTone(record_type)`.
  - `TopCountiesBars.tsx` — horizontal bars (Recharts `BarChart` layout="vertical"), teal bars.
  - `SkipTraceRate.tsx` — big stat (enriched/total %) + a thin phone/email mini-bar.
- Each card handles all **four UI states** (loading skeleton / error `ErrorState` + retry / empty
  "no leads yet, run a scraper" / data) per the house rule.
- Layout: a new **Analytics row** in `dashboard/page.tsx` placed **above** the existing bento grid,
  **below** the `StatCards` KPI strip. Responsive: `grid-cols-1 lg:grid-cols-2` (trend spans full
  width on its own row; the other three share the grid) — exact grid finalized in the plan.
- One react-query key `["analytics", window]`, `enabled: !!session`. No polling (per the
  no-polling rule); refetch-on-window-focus is fine.

## 4. Data flow

`dashboard/page.tsx` → `useQuery(["analytics", window], () => getAnalyticsSummary(window))` → one
GET → backend runs 4 user-scoped aggregates over the windowed `results` → JSON → four presentational
cards each read their slice. Window toggle lives in `LeadsTrendChart` and lifts state to the page so
the single query key changes (one refetch drives all cards, or scope the toggle to the trend only —
finalized in the plan; default: toggle re-queries the whole summary).

## 5. Sequencing (phased, gated)

**Phase 3a — backend** (`web-scrapper-automation`, branch off `main`):
1. Schema + `analytics.py` endpoint + register router.
2. Index migration (concurrent).
3. Tests (`tests/` — real settings/DB per testing rule; CI arbitrates DB-backed tests).
4. Self-review + Master Security §14 + Codex `review --base main`. Any Crit/High = NO-GO.
5. Merge → Railway deploy → confirm `/health` + a live `/analytics/summary` call → regen OpenAPI
   (`schema/openapi.json`) committed.

**Phase 3b — frontend** (`bridgeleads-web`, branch off `master`):
1. `recharts` + shadcn chart; `getAnalyticsSummary` + regen `api-types`.
2. Four chart cards (each four-state).
3. Wire the analytics row into `dashboard/page.tsx`.
4. `tsc --noEmit` + `eslint` clean; Codex review; visual check (headless browse — headed is broken on
   this box). Any P1/High = NO-GO.
5. Merge to `master` → one Vercel prod deploy → verify prod green.

## 6. Testing

- Backend: endpoint tests asserting (a) `user_id` isolation (a second user's leads never appear),
  (b) window bounding, (c) `is_duplicate` exclusion, (d) zero-fill density of `trend`, (e) top-8 +
  "other" folding for counties, (f) divide-by-zero guard on an empty account → all zeros, not 500.
- Frontend: each card renders loading/error/empty/data; window toggle swaps the query key.
- Proof-of-work (CLAUDE.md): a live `/analytics/summary` request/response on a real account + a
  screenshot of the rendered charts.

## 7. Security review hooks (§ from the pack)

- **Multi-tenancy** (the big one): every aggregate `WHERE user_id = :uid`; verify under RLS FORCE that
  cross-tenant rows are impossible even if a filter were dropped. Add the isolation test.
- **No PII leak**: aggregates count presence only; never select/return decrypted `phone`/`email`.
  Response carries only counts + record_type/county labels + dates.
- **Input validation**: `window` is a constrained enum (`30|90`); reject anything else (422), never
  interpolate it into SQL (parameterized interval).
- **Errors**: any failure returns a reference id, never a raw DB error / stack (house rule).
- **DoS/cost**: the composite index + window bound cap the scan; no unbounded full-table aggregate.

## 8. YAGNI / explicitly out of scope

- No precomputed rollup table / worker job (Approach B) — revisit only if the on-demand aggregate is
  measurably slow at real tenant scale.
- No per-chart endpoints (Approach C).
- No CSV export of analytics, no custom date ranges beyond 30/90, no drill-downs — future if asked.
- The admin **activation funnel** (`getActivationFunnel`) stays an admin metric; not on this
  per-tenant dashboard.

## 9. Open risks

- Join cost `results→jobs→scraper_configs` at high tenant volume — mitigated by the window + index;
  measure on the live call before frontend work.
- `skip_trace` phone/email presence on encrypted columns — confirm `IS NOT NULL` works on the
  `EncryptedString` SQL type (ciphertext non-null) OR use `phones`/`emails` JSON length; pick the one
  that doesn't trigger decryption. **Verify in 3a task 1.**
