# HANDOFF: skip trace for already-delivered leads, follow-ups (2026-09-19)

> **STATUS UPDATE (2026-09-19, later session). Steps 1, 2 and 4 of §4 are DONE:**
> Codex r2 GATE PASS; BE #344 merged `5bd9c59` (Railway deploy success, migration 097 verified
> live); FE #157 merged `6c435d0` (Vercel deploy success). Login "regression" root-caused to our
> own mount guard in `app/(auth)/login/page.tsx` (dev only), fixed `0c6eaea` + next-auth pinned
> `8257e8d` on `chore/security-deps-2026-09-18`, still LOCAL ONLY, awaiting the owner's go to
> push. Step 3: owner chose a new custom-range run (owner triggers it). See the 2026-09-19
> journal entry for details.

Read this top to bottom before acting. Everything here is verified unless marked otherwise.

## 1. The goal (owner's words, condensed)
Owner report: run 1 with skip trace OFF delivered 38 leads; the same range run again with skip
trace ON said "38 already delivered" and traced none of them. **Delivered and traced are separate
facts.** Dedup must stop re-delivery/re-billing, never stop enrichment. Requirements: distinguish
never-attempted / hit / miss / failed; reuse fresh answers (90-day rule, owner-confirmed); never
pay Tracerfy twice (incl. concurrency); record quota never charged for already-delivered leads;
tenant isolation; CSV carries new contacts; UI explains the outcome; no em dashes in user-facing
copy; Codex reviews every build; security Master Review (§14) + Pre-Launch (§15).

## 2. What is DONE and LIVE in production
- BE #342 (`b4b648f` on main) + FE #156 (`d5d1b3a` on master), merged + deployed 2026-09-18.
  Already-delivered (`prior_run`) leads are now skip-traceable; one predicate
  `skip_trace_eligible_condition()` in `src/api/results_category.py`; dispatcher idempotency
  (known-answer sweep, in-flight hold with no time limit, advisory lock `_CLAIM_LOCK_KEY`);
  `AlreadyDeliveredContacts` summary on the Already delivered tab. Codex: FAIL, FAIL, PASS.
- Journal entry for that work: `docs/BUILD_JOURNAL.md` (2026-09-18, "Already delivered is not
  already traced"). Plan: `tasks/todo-enrich-already-delivered.md`.

## 3. What is OPEN (this branch)
| Repo | Branch | Worktree | PR | Head |
|---|---|---|---|---|
| BE | `feat/skip-trace-provenance` | `C:/Users/Windows/bl-wt-prov` | #344 OPEN | `0bf0468` (+ this doc) |
| FE | `feat/skip-trace-provenance` | `C:/Users/Windows/bl-wt-prov-fe` | #157 OPEN | `cae23cc` |
| FE | `chore/security-deps-2026-09-18` | `C:/Users/Windows/bl-wt-deps-fe` | none, LOCAL ONLY | `620939e` (WIP) |

Plan + Codex dispositions: `tasks/todo-skip-trace-followups.md`.
Security record: `docs/security/REVIEW-2026-09-18-skip-trace.md`.

### Changes on #344 (BE)
- `alembic/versions/097_results_skip_trace_source.py`: `results.skip_trace_source` VARCHAR(16)
  NULL + CHECK (`lookup`|`reused`), lock_timeout, CHECK NOT VALID then VALIDATE in autocommit
  with its own lock_timeout (Codex r1 fix).
- `src/db/models.py`: column.
- Writers (same statement as the hit/miss): `src/workers/tracerfy_ingest.py` -> `lookup`;
  `src/workers/tasks_helpers/enrich.py` reuse stmt 1 (CASE on the exact copy predicate) + stmt 2 +
  enqueue cache hit -> `reused`; `src/workers/skip_trace_dispatcher.py` known-answer sweep ->
  `reused`.
- Retention fix (§14 finding, Medium): copies set `skip_trace_attempted_at` = cache `fetched_at`,
  not now (it is the 365-day PII purge clock). Commit `b429458`.
- API: `src/api/schemas.py` `AlreadyDeliveredContacts.reused` (subset of found+none_found, NOT a
  bucket); `src/api/routes/jobs.py` counts it in the same statement. `schema/openapi.json` +7/-1
  (the -1 is a JSON comma).
- Tests: `tests/test_skip_trace_already_delivered.py` (31, provenance asserted per writer),
  `tests/test_skip_trace_tenant_gates.py` (5: POST /jobs, PATCH /scrapers, DELETE /jobs,
  dialer-replay: 404 AND no side effect; injected state rejected 422). Mutation-checked.
### Changes on #157 (FE)
- `app/(dashboard)/results/[id]/_components/DeliveredLookupSummary.tsx`: "N came from an earlier
  lookup, so no new lookup was bought." when `reused > 0`. Types regenerated.

### Verification done
Full BE suite 4178 passed (9 Stripe tests in `test_plan_entitlement_audit.py` /
`test_promo_access.py` fail locally AND on untouched main: rig env, they pass in CI). ruff clean;
`python scripts/export_openapi.py --check` OK; FE tsc + eslint clean; Playwright (Chromium) showed
the reused line with 0 lookups queued, usage unchanged.

## 4. NEXT STEPS (in order)
1. **Codex round 2 on #344/#157.** It was blocked by Codex's usage limit ("try again at 10:12 AM").
   Prompt file ready: `<old scratchpad>/codex_prov2.txt` (see §7). Run it; reconcile findings per
   `.claude/rules/codex-collaboration.md`; fix any P1; re-review until `GATE: PASS`.
2. Then ask the owner to approve merging #344 then #157 (merge BE first; FE CI drift gate reads BE
   main; re-run FE check after, raw.githubusercontent may be cached ~5 min). Railway runs
   `alembic upgrade head` on boot: migration 097 deploys with #344. Confirm deploy:
   GitHub deployment statuses for the merge sha + `https://api.bridgeleads.io/health` 200.
3. **Owner's 38 leads (needs an owner decision; spends real Tracerfy credits):** run
   `d3298b54-9c52-419a-8c93-08b2c33be943` (Pierce probate, user `b6d2095d-d33c-4d46-841f-4f835032f563`,
   agency). Read-only snapshot: 44 already-delivered rows, all `not_attempted`, 6 without address
   (tab shows 38), 1 reusable now, 0 ATIP; at most 37 paid lookups. 🛑 Its scraper is
   `since_last_run`, so re-running it will NOT bring these back. Options: (a) owner starts a new
   run with a custom range Aug 18 to Sep 17 and skip trace on; (b) a targeted backfill script for
   that job's eligible rows (must reuse the enqueue logic, be Codex-reviewed, dry-run first).
   Afterwards compare against the frozen snapshot (statuses, `skip_trace_source`, usage delta).
4. **Critical FE dependency advisories (live in prod):** next 16.1.7 (unauthenticated RCE,
   middleware/proxy bypass) and next-auth 5.0.0-beta.30. Branch `chore/security-deps-2026-09-18`
   bumps to next 16.3.5 + next-auth ^5.0.0-beta.32: npm audit 0 critical/0 high, tsc/eslint/
   `next build` pass, BUT **login regresses**: credentials callback 200, session valid (/dashboard
   loads if visited), yet `router.push("/dashboard")` in `app/(auth)/login/page.tsx:133` never
   lands, user stays on /login. Same rig on 16.1.7 + beta.30 redirects in ~6 s. Next: isolate
   which package (try next 16.3.5 + beta.30, and 16.1.7 + beta.32), check `proxy.ts` middleware
   vs Next 16.3 RSC navigation, fix, re-verify login in Chromium, then PR. Do NOT push the WIP.
5. Pre-existing §15 FAILs owned by the owner: API has no HSTS and the rate limiter is dead
   (`100.64.0.0/10` CGNAT missing from `_TRUSTED_PROXY_NETWORKS`); order agreed 09-16: close
   direct origin access (Cloudflare is bypassable) first, then trust the proxy range.
6. After merges: journal entry for the follow-ups; update memory
   `project_enrich_already_delivered_2026_09_18.md`.

## 5. Failed attempts / dead ends (do not repeat)
- My first audit claimed the Tracerfy CSV host was not pinned; wrong. `_host_is_tracerfy()` in
  `tracerfy_ingest.py` pins it (tested). Read the caller, not only the helper.
- Bash heredocs containing `'''`/nested quotes get truncated: write patch scripts with the Write
  tool and run them.
- Bash cwd resets to the OneDrive checkout: a test glob passed to a wrapper expands there (ran 193
  instead of 314 tests once). Always `cd <worktree> &&` first.
- A mutation marker `# MUTATION` inside a SQL string breaks the SQL (Postgres uses `--`), which
  fakes a pass. Use `-- MUTATION` in SQL.
- Playwright login right after `next dev` starts can fill before hydration (silently lost): wait
  5 s after goto /login and check the URL left /login.
- `JobCreate` ignores extra fields on purpose (API-key callers); do not add extra="forbid" there.
- Querying worker-only tables (`pending_skip_trace_rows`, `delivered_records`, `skip_trace_cache`)
  from an API route passes tests and 500s in prod (no grant for `bridgeleads_app`).

## 6. How to run things (Windows box)
- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python` (Anaconda is gone).
- NEVER bare `pytest` in the repo (its .env is PRODUCTION). Use an isolated DB:
  `TEST_DATABASE_URL=postgresql+asyncpg://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_prov_test`,
  `TEST_DATABASE_URL_SYNC=postgresql+psycopg2://...same...`, DATABASE_URL(_SYNC) = those,
  `REDIS_URL=redis://127.0.0.1:6379/12`, `SECRET_KEY=<any 32+ chars>`, `STRIPE_SECRET_KEY=sk_test_fake`,
  `ENVIRONMENT=test`, then `cd C:/Users/Windows/bl-wt-prov && alembic upgrade head &&
  python -m pytest -m "not integration" -q -p no:cacheprovider -o addopts="" <files>`.
  Full suite: 4 foreground batches (memory is tight).
- Codex (Windows): feed code inline, forbid shell/files, `codex exec -c 'model_reasoning_effort="high"'
  -c 'mcp_servers={}' --skip-git-repo-check - < prompt.txt`.
- Prod read-only access: `railway run` from the OneDrive main checkout (linked to
  bridgeleads-production). Only read-only transactions; no PII in output.
- FE local E2E: throwaway `.env.local` (NEXT_PUBLIC_API_URL, AUTH_SECRET/NEXTAUTH_SECRET >= 32,
  AUTH_URL, AUTH_TRUST_HOST=true), delete after; Playwright `new_context(bypass_csp=True)`.
  Seeded local user: `enrich.e2e@bridgeleads-local.com` / `E2eLocal!2026` in DB
  `bridgeleads_enrich_e2e_test` (redis 14), LOCAL ONLY.

## 7. Session artifacts (on disk, old session scratchpad)
`C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/ff73458d-54bc-402c-b68c-61def5dcc209/scratchpad/`
- `codex_prov2.txt`: ready Codex round-2 prompt (regenerate if the diff changed).
- `prod_precheck.py`: read-only prod pre-check (`railway run python prod_precheck.py <job_id>`);
  `prod_frozen_d3298b54-9c52-419a-8c93-08b2c33be943.json`: frozen snapshot of the 44 rows.
- `penv.sh` / `ppt.sh`: test env + runner for this worktree. `pe2e_env.sh`, `pe2e_seed.py`,
  `pe2e_run3.py`, `pe2e_ui.py`: local E2E rig. `shots/`: screenshots.
