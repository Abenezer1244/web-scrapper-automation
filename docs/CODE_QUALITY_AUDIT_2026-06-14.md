# BridgeLeads — Code Quality & Maintainability Audit (2026-06-14)

**Scope:** both repos — backend `web-scrapper-automation` (FastAPI/Celery/Playwright) + frontend `bridgeleads-web` (Next.js 16 App Router).
**Method:** 2 independent Claude analysts (one per repo) + 2 independent **Codex** verification passes that re-grepped the repos to confirm/refute the risky deletions. Findings below are the **reconciled** set; where Codex and Claude agreed it's high-confidence, Codex-only catches are marked `[Codex]`.
**Mode:** READ-ONLY. Nothing was deleted. This is a plan for your sign-off.

---

## Headline verdict

Both codebases are **cleaner than a typical "audit me" target**:

- **Backend:** `ruff --select F401,F811,F841` passes **clean** (zero unused imports/vars/redefs). Zero dead scrapers, zero dead Celery tasks, zero unused `requirements.txt` deps, Selenium fully removed. The cost is **clutter + a few small dups**, not rot.
- **Frontend:** `tsc --noEmit` **clean**, but there is **NO lint gate at all** (no ESLint config, no `lint` script) — which is exactly how the dead `components/landing/`, ~37 unused `ui/*` wrappers, and 6 stray junk files accumulated unnoticed. The frontend holds the **bulk of the genuinely removable code (~5–7k LOC + 6 deps).**

**Biggest single win:** delete the frontend's dead UI surface (landing dir + dead `ui/*` wrappers + their deps) and add an ESLint `no-unused-vars` gate so it can't recur.

**Biggest trap (avoid):** do **not** bulk-delete backend `scripts/` — it hides live RLS/security/ops tooling. Move only the one-off probes/diags.

---

## Recommended cleanup plan (phased, safe — each phase is independently shippable)

> Discipline for every phase: one repo, ≤5 files of *consequence* per commit, run the build/lint gate after each batch, commit atomically. Frontend gate after each delete batch: `npx tsc --noEmit && npm run build`. Backend: `python -m ruff check src/ && pytest -q`.

**Phase 1 — Frontend zero-risk deletions (~5–7k LOC, 6 deps)**
1. Delete 6 stray junk files (shell-redirect artifacts) at repo root + `tasks/todo.md` scaffolding (gitignore the hook that spawns them).
2. Delete `components/landing/` (9 files), `components/{run,scraper,stat}-card.tsx`.
3. Delete the ~37 dead `components/ui/*` wrappers **(keep the 14 live ones — list below)** + cascade `components/ui/falling-pattern.tsx` + `hooks/use-mobile.ts`.
4. Remove 6 now-orphaned deps from `package.json`: `recharts, cmdk, vaul, embla-carousel-react, react-day-picker, react-resizable-panels` (+ `@tanstack/react-query-devtools`). Run after step 3 so `tsc`/`build` proves no consumer.
5. Remove dead exports: `lib/api.ts` `listBatches`+`getSampleData`; `lib/utils.ts` `canUseWebhook`+`canUseSkipTracing`; `lib/auth.ts` re-exported `signIn`/`signOut` (keep the file); prune the 12 unused `lib/types.ts` types.

**Phase 2 — Frontend guard rail (prevents recurrence)**
6. Add ESLint (`eslint-config-next` + `@typescript-eslint/no-unused-vars`) and a `lint` script; optionally `knip` in CI. This is the highest-leverage item — without it, dead code silently returns.

**Phase 3 — Backend clutter + micro-dedup (low risk)**
7. `scripts/`: move **one-off probe/diag** files to a gitignored `scripts/scratch/` (NOT delete-and-forget); **commit** the keep-list ops/security utilities so they stop reading as clutter; gitignore `scripts/audit_out*/` + the stray `scripts/test_results_2026_05_02.txt`. (Keep-list below.)
8. Consolidate the 3 small dups (§B3): `_redis()` → `src/utils/redis.py`; the two address normalizers → one `src/utils/address.py` **with a golden test** (it's a cache/identity key); the date-window `strptime` → `BaseScraper._parse_date_window`.
9. Remove `schemas.py::ProgressEvent` if grep still shows zero refs `[Codex]`.

**Phase 4 — Verify-then-remove (needs a prod check first)**
10. `range_mode` legacy schedule alias — run `SELECT count(*) FROM scraper_configs WHERE schedule ? 'range_mode'` (and scheduled batches); if 0, drop the fallback in `jobs.py` + `tasks.py`.
11. `webhooks.py` legacy Tracerfy path-secret route — delete **only after** Tracerfy is migrated to the header route + secret rotated (existing ops follow-up).

**Phase 5 — Tech-debt / complexity (separate, phased; NOT a dead-code task)**
12. Decompose the monoliths (see §G). Apply the CLAUDE.md "Step 0" rule (strip dead props/imports first), split into ≤300-LOC sub-components/modules, one file per commit. Do not batch.

---

## A. Frontend — dead components (highest value)

### A1. `components/landing/` — entire directory dead (9 files, ~1,500+ LOC)
- **Why:** `app/(marketing)/page.tsx` is a single 1,819-LOC `"use client"` monolith that **inlines** hero/features/pricing/etc. The extracted `landing/*` components were never wired in. **Claude + Codex CONFIRM:** zero `@/components/landing/*` imports anywhere.
- **Impact:** ~1,500+ LOC removed; ends the "two copies of the landing page" confusion.
- **Risk:** LOW. **Cleanup:** delete the dir. (Either adopt these components OR keep the inline page — don't keep both; deleting the unused dir is the smaller change.)

### A2. `components/{run,scraper,stat}-card.tsx` — imported nowhere
- **Why:** `RunCard/ScraperCard/StatCard` — grep finds only their own definitions (Claude + Codex CONFIRM).
- **Impact:** 3 files. **Risk:** LOW. **Cleanup:** delete.

### A3. ~37 unused `components/ui/*` shadcn/Base-UI wrappers
- **Why:** Generated by the `shadcn` CLI, never consumed. The app uses Base UI primitives / the underlying packages directly (e.g. `Toaster` imported straight from `sonner` in `providers.tsx:7`, bypassing the `ui/sonner.tsx` wrapper). **Codex pinned the split** by grep:
  - **DELETE (dead):** `accordion, alert, alert-dialog, animated-tabs, aspect-ratio, avatar, breadcrumb, button-group, calendar, carousel, chart, collapsible, combobox, command, context-menu, dialog, direction, drawer, dropdown-menu, field, hover-card, input-group, item, kbd, menubar, native-select, navigation-menu, pagination, popover, progress, radio-group, resizable, scroll-area, select, separator, sheet, sidebar, sonner, spinner, switch, tabs, textarea, toggle, toggle-group`. Big ones: `sidebar` (723), `chart` (373), `combobox` (297), `menubar` (280), `context-menu` (271), `dropdown-menu` (268).
  - **CASCADE dead** once the above go: `components/ui/falling-pattern.tsx`, `hooks/use-mobile.ts` (only imported by dead `sidebar`).
  - **KEEP (live — `[Codex]` verified imported):** `button, input, label, input-otp, checkbox, badge, card, empty, skeleton, spotlight-card, progress-circle, animated-counter, table, tooltip`.
- **Impact:** large LOC; unblocks the 6 dep removals in A4.
- **Risk:** LOW–MED. The surprising ones (`dialog, select, popover, tabs`) read as "surely used" — Codex grep says the app uses Base UI directly, so the *wrappers* are dead. **Mitigation:** delete in batches and run `tsc + build` after each; if any batch breaks, that wrapper was live — restore it. Also **verify against in-flight branches** (CLAUDE.md notes several unmerged) before bulk delete.

---

## B. Backend — findings

### B1. `scripts/` clutter — 130 files + 4 dirs, MIXED (do NOT bulk-delete) `[Codex caught the trap]`
- **Why:** ~49 untracked one-off `probe_*` (Spokane Cloudflare investigation, island, etc.), `diag_*`, `test_*`/`e2e_*` live walkthroughs, plus generated `audit_out*/` dirs and a stray `test_results_2026_05_02.txt`. None are imported by `src/`/`workers/` or deployed (Railway ships tracked `main`).
- **CRITICAL:** the dir **also** holds live ops/security tooling that must be preserved: `apply_rls_*`, `provision_rls_roles.sql`, `_cutover_*`, `backfill_*`, `reencrypt_derived_key_pii.py`, `construct_fek_rotation.py`, `force_rls_nts_notices.py`, `verify_*`, `generate_break_glass.py`, `reset_user_mfa.py`, `onboard_customer.py`. Several tie to **open incident follow-ups** (the 2026-06-13 key-drift family).
- **Impact:** moving the one-offs clears ~5–6k LOC of noise from the working tree → "what's actually shipped" becomes obvious.
- **Risk:** LOW if you move (not delete) and preserve the keep-list. **Cleanup:** `git status --porcelain scripts/ | grep '^??'` → move `probe_*`/`diag_*`/`e2e_*`/`test_*.txt` one-offs to gitignored `scripts/scratch/`; **commit** the ops/security utilities; gitignore `scripts/audit_out*/`.

### B2. Legacy code
- **`webhooks.py` Tracerfy path-secret route** — self-labeled LEGACY; header route is the replacement. **DO NOT remove yet** (live inbound path) — gated on the ops migration + secret rotation.
- **`jobs.py` + `tasks.py` `range_mode` schedule alias** — old key kept alive via `schedule.get("date_range_mode") or schedule.get("range_mode")`. Source-live; `[Codex]` found no seeded migration rows, but **live DB not verified** → query before dropping the fallback.
- **Selenium** — fully removed; only the legitimate `navigator.webdriver` anti-detection JS remains. No action.

### B3. Duplicate logic (Claude + Codex CONFIRM — all real, all small)
- `skip_trace.py::_normalize_address` vs `property_identity.py::normalize_address` — near-identical normalize; different *keys* (cache vs identity) → divergence-bug risk. Consolidate to `src/utils/address.py` **with a golden test** (treat like the frozen `legacy_strong_signature`).
- `tasks.py::_redis` == `ai/cache.py::_redis` (identical body) → `src/utils/redis.py`.
- `datetime.strptime(date_from, "%m/%d/%Y")` repeated in ~10 county/template scrapers → `BaseScraper._parse_date_window`.
- **Note:** Redis client construction appears in ~10 places but already routes SSL config through the single `settings.redis_kwargs()` — **shallow, leave it**. CSV building is **already** centralized in `lead_export` — good, no action.

### B4. Dead code `[Codex MISSED-by-Claude catch]`
- `src/api/schemas.py::ProgressEvent` — only the definition found, no references. **Impact:** ~10 LOC. **Risk:** LOW (verify grep first). **Cleanup:** remove.

### B5. Registration risk (not dead) `[Codex]`
- `src/workers/dialer_outbox.py` defines a real enqueued task but is **not** in Celery `include=[...]`; it's imported lazily before some `.delay()` paths. **Not dead**, but a worker restart / import-order change could drop dispatch. **Cleanup:** add it to the Celery `include` list for robustness (defensive, not a deletion).

---

## C. Dead exports / types (frontend)
- `lib/api.ts` — `listBatches` (:246), `getSampleData` (:614): 0 callers. Remove.
- `lib/utils.ts` — `canUseWebhook` (:122), `canUseSkipTracing` (:126): 0 callers. Remove. (`RUNNING_STATUSES` is used internally by `isRunning` — keep; Claude over-flagged it, Codex didn't confirm it dead.)
- `lib/auth.ts` — keep the file (live via `middleware.ts` + the nextauth route); remove only the unused re-exported `signIn`/`signOut`.
- `lib/types.ts` — 12 unused exported types (`ExportFormat, Plan, ScraperConfigFields/Enrichment/Schedule, EnrichmentData, SkipTraceStatus, PhoneContact, OnboardingNextAction, SampleRecord, BatchChildSummary, BatchFailedChild`). Prune; **verify** none are an intended public contract.

## D. Unused dependencies
- **Frontend remove (6, after A3):** `recharts, cmdk, vaul, embla-carousel-react, react-day-picker, react-resizable-panels`. Plus `@tanstack/react-query-devtools` (0 refs).
- **Frontend KEEP `[Codex]`:** `@remotion/cli` (RISKY to drop — Remotion compositions in `remotion/` are **live**, lazy-loaded in the marketing page; CLI may be used for manual renders) and `shadcn` (the generator + `components.json` schema). Move both to `devDependencies` at most.
- **Backend:** none unused — every `requirements.txt` package is imported or a required runtime/test/driver dep (lxml, pypdf, 2captcha, pandas, openpyxl, httpx[test], asyncpg/psycopg2 drivers, etc. all verified).

## E. Redundant DB queries / API calls
- **None actionable.** The `db.refresh()` after writes in `tasks.py`/`auth.py` are **deliberate** (async-SQLAlchemy `MissingGreenlet`/`onupdate` trap — documented in project memory). No N+1 found in the job pipeline (the near-loop `db.execute` is a single bulk fetch). Frontend fetch/error/toast handling is already centralized (`lib/api.ts apiFetch`, `lib/errors.ts`). No action.

## F. Files abandoned / disconnected
- **Frontend:** 6 shell-fragment junk files at root (`0`, `e.children.length`, `'`, `{const`, `{try{const`) — `0` even contains a Windows error string from an errant hook redirect. Delete + gitignore the source hook. `tasks/todo.md` + `.impeccable.md` — decide commit-or-gitignore.
- **Backend:** covered in B1 (the untracked one-offs). Also `src/graphify-out/` is a ~26 MB local cache living under `src/` (gitignored) — fine, but consider moving out of `src/`.

## G. Overly complex (refactor candidates — NOT dead, do separately)
| Repo | File | LOC | Note |
|------|------|-----|------|
| FE | `app/(dashboard)/scrapers/new/page.tsx` | 2,138 | Multi-step wizard (Single/Batch). Extract steps to `_steps/`. |
| FE | `app/(marketing)/page.tsx` | 1,819 | Inlines the dead `landing/*`. Pick one. |
| FE | `app/(dashboard)/settings/page.tsx` | 1,339 | Extract tabs. |
| FE | `app/(dashboard)/results/[id]/page.tsx` | 1,290 | Extract row/filter logic. |
| BE | `src/workers/tasks.py` | 1,786 | `run_scrape_job` state machine — hottest path, heavily Codex-gated. Phase carefully. |
| BE | `src/workers/scheduler.py` | 1,586 | 21 beat tasks in one module. |
| BE | `src/api/routes/auth.py` | 1,513 | |
- **Risk:** MED. These are load-bearing; treat as a dedicated phased effort, not a sweep.

---

## H. DO-NOT-DELETE / false-positive guards (the safety net)
Both reviewers explicitly cleared these as **live despite looking unused** — do not let a future sweep touch them:
- **Backend:** all Alembic migrations; all 11 allowlisted scraper modules + 8 URL-detected templates (dynamic `importlib`/connector-driven); all decorator-registered routes; `db.refresh()` calls; `webhooks.py` legacy route (until ops migration); `range_mode` (until DB queried); the `scripts/` security/ops keep-list (B1).
- **Frontend:** the 14 live `ui/*` wrappers (A3); `lib/auth.ts` (file); `remotion/` (lazy-loaded); all App Router convention files (`layout/error/not-found/global-error/route`); `providers.tsx`; live shared components (`error-state, empty-illustration, theme-toggle, quota-upgrade-banner, offline-banner, onboarding-banner, log-stream, new-badge, legal/legal-shell, settings/security-tab, motion/variants`); `@remotion/cli` + `shadcn` deps.

---

## Estimated impact
- **Frontend:** ~5,000–7,000 LOC removable (landing + ~37 ui wrappers + cards + dead exports) + 6 deps dropped + a lint gate added. **High value, low risk.**
- **Backend:** ~5–6k LOC of script noise relocated out of the working tree, ~40 LOC of dups consolidated, ~10 LOC truly dead (`ProgressEvent`). **Mostly hygiene; the code itself is already lean.**
- **Net:** materially smaller, clearer frontend; a cleaner backend working tree; and (via the ESLint gate) prevention of recurrence — which is the durable win.
