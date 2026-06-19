# Dashboard Analytics — Darkmatter Phase 3 (design)

**Date:** 2026-06-19
**Status:** approved (brainstorm) — **Codex consult folded in (2026-06-19)** — pending user spec review
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

`GET /analytics/summary?window=30` (allowed: `30`, `90` only; default `30`; anything else → `422`,
never coerced). Auth = standard JWT/API-key dependency. **Mandatory `user_id` query filter on every
aggregate** (RLS belt + filter suspenders) — and on every *joined* table too (see below). All
aggregates exclude duplicates (`is_duplicate = false`).

**Window + timezone (Codex C/H):** `date(created_at::timestamptz)` would group by the DB session TZ
(UTC on Supabase), so a Pacific user's "today" leaks across midnight. Group by
`(created_at AT TIME ZONE :tz)::date` where `:tz` = a new `ANALYTICS_TIMEZONE` setting (default
`America/Los_Angeles`; in `settings.py` + `.env.example`). The **window is the last N calendar days
*including today* in that TZ** (today + prior N-1), bounded as
`created_at >= (current_date_in_tz - (N-1) days)` — documented explicitly so the chart is calendar-day,
not a rolling 30×24h window.

Response (`AnalyticsSummary` Pydantic schema) — **stable shapes**: `trend` always dense (zero-filled),
skip-trace always returns its fixed buckets, `by_*` are `[]` on a new tenant (Recharts must not jitter):

```jsonc
{
  "window_days": 30,
  "timezone": "America/Los_Angeles",
  "trend":          [{ "date": "2026-06-01", "leads": 124 }, ...],   // dense, zero-filled, today inclusive
  "by_record_type": [{ "record_type": "probate", "leads": 980 }, { "record_type": "unknown", "leads": 12 }],
  "by_county":      [{ "county": "king", "state": "WA", "leads": 4210 }, { "county": "other", "state": null, "leads": 70 }],
  "skip_trace":     { "total": 2600, "enriched": 1840, "phone_pct": 64, "email_pct": 52 }
}
```

Four aggregates, each its own `SELECT`, run **sequentially on the one async session** (NOT
`asyncio.gather` — the async session is not concurrency-safe):

1. **trend** — `results` ONLY (no join), group by the TZ-day above, count. Zero-fill missing days **in
   Python** so the x-axis is continuous.
2. **by_record_type** — `results LEFT JOIN jobs ON (jobs.id = results.job_id AND jobs.user_id = :uid)
   LEFT JOIN scraper_configs ON (scraper_configs.id = jobs.scraper_config_id AND
   scraper_configs.user_id = :uid)`, group by `coalesce(record_type, 'unknown')`, count. **LEFT JOIN +
   `unknown` bucket** so leads whose job/config is missing (deleted, NULL `scraper_config_id`, batch
   edge cases) are still counted — an inner join would silently undercount (Codex C/H).
3. **by_county** — same LEFT JOIN, group by `(lower(county), upper(state))`, count, order **count desc
   then county asc** (deterministic), top 8; remainder folded into one `{"county":"other","state":null}`
   bucket and missing metadata into `{"county":"unknown","state":null}` **in Python**. State is in the
   key so King-WA ≠ King-TX (matters under national expansion; Codex).
4. **skip_trace** — `results` ONLY, in window: `total` = all non-dup leads; `enriched` =
   `skip_trace_status='hit'`; `phone_pct`/`email_pct` = leads whose **scalar `phone`/`email`** (the
   canonical primary = `phones[0]`, per multi-contact) `IS NOT NULL` over total. Percentages in Python
   (divide-by-zero → 0). `skip_trace_status` is `NOT NULL default 'not_attempted'` so no NULL bucket,
   but the response returns the **fixed status set** with zeroes for absent ones.

New file `src/api/routes/analytics.py`; router registered in `main.py`. Schema in `src/api/schemas.py`.
The `phone`/`email` columns are `EncryptedString`; **count presence WITHOUT decrypting** via `phone IS
NOT NULL` on the scalar column (ciphertext is still non-null). ⚠️ **Task-1 verification (Codex):**
confirm the skip-trace pipeline stores SQL `NULL` (not an encrypted empty string) when there's no
result — if it can store `""`-as-ciphertext, switch to a normalized check. Do **not** inspect the
encrypted `phones`/`emails` JSON blobs in SQL (can't, and scalar primary is canonical).

### 3.2 Index migration

New Alembic migration: **partial** composite index
`ix_results_user_created` on `results(user_id, created_at) WHERE is_duplicate = false` — partial
because *every* analytics query filters `is_duplicate = false`, so the index matches the predicate
exactly (Codex). Created **`CONCURRENTLY`** with `statement_timeout=0` (the `results` table is large —
see the landmine memo [[incident_backfill_blocks_migration]]: never block on a big-table index). Run
via `scripts/migrate.py` (advisory lock). The join hits `jobs.id`/`scraper_configs.id` (PKs) and the
tenant predicates use their existing `user_id` indexes (from H1) — verify in task 1, add
`jobs(user_id, id)` only if the planner needs it (likely not; the recent-window `results` filter is
the selective leg). RLS: `results` is already an enforced/FORCE table from H1 — no new policy needed;
the endpoint runs on `bridgeleads_app` (NOBYPASSRLS — verify) and the `user_id` filter + existing
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

- Backend: endpoint tests asserting (a) `user_id` isolation (a second user's leads never appear —
  test under RLS FORCE), (b) calendar-day window bounding **in the configured TZ** incl today,
  (c) `is_duplicate` exclusion, (d) zero-fill density of `trend`, (e) top-8 + `other` folding AND the
  `unknown` bucket for a lead whose job/scraper_config is missing (LEFT-JOIN correctness — trend total
  ≥ sum of resolved-type leads, reconciled by `unknown`), (f) divide-by-zero guard on an empty account
  → all zeros, not 500, (g) county state-collision: King-WA and King-TX stay distinct, (h) skip-trace
  presence counts the scalar primary phone/email only.
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
- **Deferred (Codex's "biggest change"): denormalize `record_type`/`county`/`state` + `has_phone`/
  `has_email` booleans onto `results` at ingestion.** It would delete the fragile join, avoid any
  decrypt path, and keep historical analytics stable across job/config deletion. **Not now** because
  it needs a schema migration + a **backfill across the large `results` table** (the precise
  migration-landmine in [[incident_backfill_blocks_migration]]) + ingestion/worker changes — too much
  for this phase. The LEFT JOIN + `unknown` bucket addresses the correctness concern. Revisit if the
  join is measurably slow OR job/config hard-deletion proves to distort reports.

## 9. Open risks & task-1 verifications (from the Codex consult)

Resolve these in **Phase 3a task 1** before writing the aggregates:
- **Child/batch job → `scraper_config_id`:** confirm `results.job_id` points to *child* jobs that
  carry `scraper_config_id` (so record_type/county resolve). FK facts: `results.job_id` is
  `NOT NULL` + `ondelete=CASCADE` (no orphan results), so the LEFT JOIN's `unknown` bucket only catches
  jobs with NULL `scraper_config_id` / deleted configs — quantify how common that is.
- **Encrypted presence:** confirm "no phone" = SQL `NULL` (not `""`-as-ciphertext) so `phone IS NOT
  NULL` is a true presence test; otherwise normalize.
- **`bridgeleads_app` is NOBYPASSRLS** (H1 says so) — assert in the isolation test.
- **Timezone:** `ANALYTICS_TIMEZONE` default `America/Los_Angeles`; confirm Postgres has the named
  zone (`AT TIME ZONE` accepts IANA names) and the chosen window math (today + prior N-1, calendar
  days) reads right at a month boundary.
- **Join index:** EXPLAIN the live query; only add `jobs(user_id, id)` if the planner actually needs
  it (expected: the windowed partial `results` index is the selective leg).

> **Codex consult (2026-06-19) folded in:** LEFT-JOIN+`unknown` over inner join (undercount),
> per-joined-table tenant predicates, explicit TZ grouping, partial index `WHERE is_duplicate=false`,
> county+state key (collision), scalar-primary encrypted presence, sequential (not gathered) queries,
> deterministic top-8 ordering, stable zero-filled shapes. Denormalization deferred with rationale.
