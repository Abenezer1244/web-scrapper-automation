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

## Todo — all complete
- [x] Confirm plan + missing-value convention with user
- [x] BE: expose `batch_id` + opt-in `exclude_batch_children` on GET /scrapers
- [x] BE: BatchSummary.counties (aggregate reused by both pages)
- [x] BE: server-side record_type/county filters + filtered total + facets
- [x] FE: Scrapers page = one expandable row per batch
- [x] FE: batch detail filters composing with pagination + URL state
- [x] FE: record type neutral in tables, colour kept in the pie
- [x] FE: Lead Mix responsive via container queries
- [x] FE: shared <EmptyValue>; only placeholder em dashes replaced
- [x] Tests: 2264 passed + 5 new regression tests; FE tsc/lint/build clean
- [x] Playwright verification at 7 viewports against the real prod dataset
- [x] Codex review round 2 (4 findings; 2 real and fixed, 2 disproved)
- [x] Dead code: recordTypeTone narrowed (NOT deleted), 2 duplicate `dash` consts removed

## Review

### What changed
Presentation aggregation only — no data-model change. The parent/child batch model
already existed and is DB-enforced; the API simply never exposed it.

### Verified against prod (login account owns Test 9/10/11)
- GET /scrapers default -> 17 rows (6 batch children) — UNCHANGED, back-compat intact
- GET /scrapers?exclude_batch_children=true -> 11 rows, 0 children
- Scrapers page: 17 rows -> 14 (11 standalone + 3 batch)
- Test 9 = ONE row, expandable, "Batch · 2 scrapes", King, Pre foreclosure + Probate
- Test 11 (run_status=partial) renders "Completed with errors" + warning icon
- Filter proof on Test 9's real 267 leads: facets [pre_foreclosure, probate] / [king];
  155 + 112 = 267; composed filter 155; rows == filtered total; 0 non-matching rows;
  bogus filter -> 0 (not 267)
- Record type chips: 19 on screen, 1 distinct colour (was 6)

### Findings I did NOT fix (pre-existing, out of scope — reported, not hidden)
- The app shell's PRO TRIAL banner ("Upgrade now") and the dashboard KpiStrip do not
  wrap, so /dashboard and /scrapers overflow horizontally at <=1024px. Measured on
  origin/master too: /scrapers bodyScroll 703 baseline vs 696 mine; dashboard 881
  baseline vs 888 mine. Unrelated to the Lead Mix, which never overflows its card.
- `export_openapi.py --check` is not CI-equivalent in this environment: it reports
  STALE for main's own committed schema (.venv-schema has fastapi 0.137.2,
  requirements.txt pins 0.141.1).

### Landmine hit
`git stash` is SHARED across all worktrees of a repo. A `git stash push -- src/` that
staged nothing left a later `pop` applying ANOTHER session's stash into this worktree
(conflict in docs/BUILD_JOURNAL.md). Their 3 stash entries were preserved and the
conflict was reset to HEAD; my commits contain only my own files. Never `git stash`
in a shared-worktree repo.
