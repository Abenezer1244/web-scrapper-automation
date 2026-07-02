# Single "Start run" — collapse Run once / Save & run

## Problem (root cause, verified against live DB)
The wizard's last step has two buttons: **Run once** (`POST /scrapers/preview` → `active=False` config + `trigger="preview"` job) and **Save & run** (`POST /scrapers` active config + manual job). `GET /scrapers` filters `active=true`, and the dashboard's Scrapers table + ACTIVE SCRAPERS KPI derive from that list. So a "Run once" scrape runs and bills real records but is **invisible forever** (no run-history page exists). Verified: config `new test pro` `active=False`, job `daab0414` `done`, 9 records.

Core defect (Codex): `active` conflates **visible/usable** with **scheduled/recurring**. The dispatcher already separates them — `dispatch.py:99` skips `frequency == "manual"`. So `active=True` + `frequency=manual` = visible on dashboard, never auto-runs.

## Decision (user + Codex)
One **"Start run"** button. Always saves a **visible** scraper (`active=true`) and runs it once. Defaults `frequency=manual` (no surprise recurring runs); recurring stays an explicit opt-in. Delete the preview path.

## Phases (max 5 files each)

### Phase 1 — Backend (worktree off origin/main, branch feat/single-start-run)
- [ ] Delete `POST /scrapers/preview` endpoint (`src/api/routes/scrapers.py`)
- [ ] Simplify `_build_scraper_config` — drop the `active` param + preview-forces-manual block (only active=True path remains)
- [ ] Remove the `scraper_preview_created` audit usage
- [ ] Regenerate `schema/openapi.json` if route-checked
- [ ] Tests: manual active config appears in active list; dispatcher skips manual-frequency active configs (real settings, no mocks)

### Phase 2 — Frontend (worktree off origin/master, branch feat/single-start-run)
- [ ] `DeliveryStep.tsx`: remove "Run once" ghost button (single mode); rename "Save & run" → "Start run"
- [ ] `scrapers/new/page.tsx`: remove `handleTestRun`, `testRunLoading`, `previewScraper` import + props threading
- [ ] Default `frequency = manual` in the wizard (verify current default; no surprise schedule)
- [ ] `lib/api.ts`: remove `previewScraper`; regen `lib/api-types.generated.ts` (drop /preview)
- [ ] Dashboard `page.tsx`: add `refetchInterval`/invalidation so a new scraper appears without manual refresh

### Phase 3 — Data
- [ ] Reactivate `new test pro` (set `active=true`) so the user's 9-record run surfaces (frequency already manual → won't auto-run). One-row UPDATE, confirm exact action.

### Phase 4 — Verify + Codex review
- [ ] Backend: `pytest` relevant tests, `ruff`
- [ ] Frontend: `npx tsc --noEmit`, `npx eslint`/`next lint`
- [ ] `codex review` both diffs — any Critical/High = NO-GO until fixed
- [ ] Live verify in prod UI (admin acct)

## Notes / risks
- Don't touch other sessions' branches (`feat/fields-output-visibility` BE, `feat/schedule-day-picker` FE). Additive worktree only.
- api-types regen may need live main; if so, hand-edit the generated /preview removal or defer regen until backend merges.
