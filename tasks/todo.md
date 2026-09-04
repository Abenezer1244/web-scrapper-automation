# UI/UX Cleanup + Scraper Result Architecture

Branch: `feat/uiux-cleanup-batch-results` (BE + FE worktrees, isolated).
BE worktree: `C:/Users/Windows/bridgeleads-worktrees/uiux-be` (off origin/main `ea3f32f`)
FE worktree: `C:/Users/Windows/bridgeleads-web-worktrees/uiux-fe` (off origin/master `305e7a7`)

## Verified findings (evidence, not assumption)

### Item 4 — batch architecture (ground truth from prod, BYPASSRLS role)
- Parent/child model ALREADY EXISTS and is DB-enforced:
  `scraper_batches` -> `scraper_configs.batch_id` (composite tenant FK) -> `jobs` -> `results`;
  `batch_runs` = one execution.
- **Test 9 IS one batch**: `scraper_batches` `9fb8f55e-83f1-4fbc-a111-8a7f9da6f906` name "test 9 batch",
  2 children (`king/pre_foreclosure`, `king/probate`), both `batch_id=9fb8f55e`, identical `created_at`.
  Grouping uses the FK ONLY — never name/time/user/date.
- Prod shape: 33 active configs = 25 standalone + 8 batch children across 4 batches.
  Scrapers page shows 33 rows; correct is 29.
- ROOT CAUSE: `GET /scrapers` (src/api/routes/scrapers.py:88) selects all active configs with NO
  batch filter, and `ScraperConfigResponse` (src/api/schemas.py:678) does not expose `batch_id`.
- `/results` page ALREADY collapses batches correctly (listStandaloneJobs + listBatches).
- Precedent to copy: `GET /jobs?exclude_batch_children=true` + `JobResponse.batch_id` already exist.
- Combined leads are DEDUPED across children; `matched_record_types` / `source_counties` are
  bucket-AGGREGATED arrays -> one lead can belong to 2 record types at once. Filters must use array
  containment on the deduped set; per-type subtotals legitimately exceed the total.
- `_COMBINED_SQL` / `_DELIVERY_COUNTS_SQL` (src/workers/batch_export.py) are the SINGLE authoritative
  count/dedupe rule, shared with the CSV export. Do NOT create a second one.

### Item 1 — record type colors
- `recordTypeTone()` lib/utils.ts:162 maps record type -> brand color.
- Call sites: scrapers/page.tsx:250, dashboard/_components/ScrapersTable.tsx:77,
  dashboard/_components/RecordTypeMix.tsx:43,51.
- RecordTypeMix is the Lead Mix PIE CHART — per-slice color is legitimate data encoding.
  => NARROW the helper to chart use; do NOT delete it.

### Item 2 — Lead Mix responsiveness
- `RecordTypeMix.tsx:38` `<ChartContainer className="h-36 w-36 shrink-0">` = fixed 144x144, cannot
  shrink or grow. Sibling charts use `h-56 w-full` / `h-48 w-full` -> it is the ONLY chart deviating
  from the app's own convention.
- Wrapper `flex items-center gap-4` never wraps; card sits in `lg:col-span-3` of a 12-col grid, so at
  ~1024px the card is ~230px: 144px chart + gap leaves ~30px for the legend.
- Stack: Tailwind v4 (native container queries), Next 16.1.7, recharts 3.8.
  Card width comes from its GRID COLUMN, not the viewport -> container queries are the correct tool.

### Item 3 — em dash placeholders
- Legit prose em dashes dominate (~340) -> LEAVE ALONE.
- Placeholder uses: 22 `&mdash;` spans across 8 files + ~15 literal `"—"` + 3 helpers.
  Densest: ResultsTable.tsx (9), BatchLeadsTable.tsx (5), segments/page.tsx (5).
- TWO duplicated `dash` consts: LeadCards.tsx:49 (inline style) and SegmentCards.tsx:28 (className)
  -> consolidation target.
- lib/utils.ts:11/59/80 (`timeAgo`, `formatDate`, `formatDateTime`) return "—" for null.

## Codex consult (round 1) — adopted / rejected
- ADOPTED: presentation aggregation (Option A), no data-model change; do not compute batch counts
  from child jobs; severity-based status aggregation; server-side filtering; preserve deep links.
- REJECTED (independently verified as wrong for this codebase): Codex said make `GET /scrapers`
  return standalone-only BY DEFAULT. Verified that would regress two callers:
  * `components/probate-tod-notice.tsx:52` filters ALL configs for grandfathered probate — a batch
    child IS a probate config (Test 9), so the compliance notice would silently stop appearing.
  * `app/(dashboard)/scrapers/[id]/records/page.tsx:61` does `scrapers?.find(s => s.id === id)` —
    excluding children makes `config` undefined and breaks the page.
  => use the OPT-IN `exclude_batch_children` param instead (matches the `/jobs` precedent).
- NOTED, out of scope: `jobs.batch_run_id` would give per-OCCURRENCE membership (today jobs->batch
  resolves only to the parent). Not needed for this fix; recorded as a follow-up.

## Todo
- [ ] Confirm plan + missing-value convention with user
- [ ] BE: expose `batch_id` on ScraperConfigResponse + opt-in `exclude_batch_children` on GET /scrapers
- [ ] BE: batch summary aggregate (status severity + authoritative count) reused by both pages
- [ ] BE: server-side `record_type` / `county` filters on batch leads (+ matching filtered total)
- [ ] FE: Scrapers page renders one row per batch (neutral record-type chips, aggregate count/status)
- [ ] FE: batch detail — record type + county filters, composing with pagination/search
- [ ] FE: neutralize record-type color in table contexts, keep it in the pie chart
- [ ] FE: Lead Mix responsive fix via container queries + w-full convention
- [ ] FE: shared missing-value component; replace placeholder em dashes only
- [ ] Tests: BE pytest + FE typecheck/lint/build; regression tests for aggregation/counts/filters
- [ ] Playwright verification at desktop/laptop/tablet/mobile
- [ ] Codex review round 2
- [ ] Dead code removal (verified safe)
