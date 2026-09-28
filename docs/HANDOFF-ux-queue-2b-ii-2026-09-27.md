# HANDOFF: UX audit queue — 2b-ii Phase A is in PR #375, not merged (2026-09-27)

Supersedes `docs/HANDOFF-ux-queue-2026-09-27.md` for "where you are". That file still holds
the full queue, the owner's per-item workflow and older landmines; read both.

## Goal
Owner's standing request: work the BridgeLeads UX audit follow-ups **one at a time**. For EACH item:
1. Investigate the code. 2. Write the plan in `tasks/todo-*.md`. 3. **Consult Codex on the plan**
until PLAN: GO. 4. **Stop for the owner's explicit confirmation.** 5. Build with real-DB tests, each
proven RED on unfixed code (mutation-check the guards). 6. Full suite, then security review (§14).
7. **Codex diff review until GATE: PASS** (any P1 = no-go). 8. Quiesce check, then merge — merge IS
deploy (BE `main` → Railway api/worker/beat, FE `master` → Vercel). 9. Verify production directly.
Audit source: FE repo `C:/Users/Windows/bl-wt/fe-ux-audit/docs/ux-audit/UX-AUDIT.md` (49 findings) and
`phase-3.0-contracts.md` (Q1–Q6). Project rules: `CLAUDE.md` + `.claude/rules/*`.

## Where you are (updated 2026-09-28)
- **2b-ii Phase A is LIVE:** BE **#375** `ae351c4e` merged after a quiet check (api/worker/beat
  SUCCESS; evaluator read-only over all 121 prod configs: 87 can_run + 34 config_inactive, 0
  errors). **FE #167** `54bc200` merged, Vercel SUCCESS; prod verified in the owner's tab: 68/68
  rows valid under the full contract, DOM = the page's rule, 0 console errors.
  Journal entry: `docs/BUILD_JOURNAL.md` 2026-09-28.
- **2b-ii Phase B is LIVE too:** BE **#380** `405ba52c` (additive 402 code, owner chose A) and
  FE **#168** `a5ed32a` (toast names the fix). Journal: `docs/BUILD_JOURNAL.md` 2026-09-28 "The
  402 says why". **2b is DONE.**
- **NEXT = 2c** (Q1 run-count breakdown, needs a migration). Start with investigation from a fresh
  branch off `origin/main`; same workflow (plan, Codex until GO, owner confirmation). Test DB
  `bridgeleads_eligibility_test` is at migration 105.

## Queue status
| # | Item | Status |
|---|---|---|
| 1 | F-042/F-041 session reads | ✅ LIVE (FE #164) |
| 2a | F-003/Q5 one active run per scraper | ✅ LIVE (BE #367 mig 104, FE #165) |
| 2b-i | Q6 account-level `run_eligibility` on `/billing/usage` | ✅ LIVE: BE #369 `cd755883`, FE #166 `8fb25a9`, journal BE #371. Prod verified: all 7 users read-only, 0 errors; owner-authenticated `GET /billing/usage` 200s in api logs; Billing page renders "Agency 4,516 / ∞ · resets Oct 1" |
| 2b-ii A | per-scraper `run_eligibility` on GET /scrapers + POST /jobs through one evaluator | ✅ LIVE BE #375 `ae351c4e` |
| 2b-ii FE | Run now disabled with reason (+ list no longer blanks on a failed poll) | ✅ LIVE FE #167 `54bc200` |
| 2b-ii B | additive 402 `code` + `resumes_at`; FE toast "Update payment" / "Resubscribe" / "Upgrade plan" | ✅ LIVE BE #380 `405ba52c`, FE #168 `a5ed32a` |
| **2c** | **Q1 run-count breakdown (migration)** | **NEXT** |
| 2d | Q2 `already_delivered` on JobResponse | queued |
| 2e | Q4 "Lookup failed" vs "not available" | queued |
| 3–5 | batches B–E, F-043 Phase 2, Phase 4/5 | queued |

## Next step (in order)
1. 2c: investigate (FE `phase-3.0-contracts.md` Q1), write its plan in `tasks/`, Codex until
   PLAN: GO, **stop for the owner**. Then build, real-DB tests RED on unfixed code, full suite (8
   parts, each backgrounded and awaited one at a time), §14, Codex diff GATE: PASS, quiesce, merge
   (migration: quiesce is mandatory), prod verify, FE PR. FE stubs for 2b-ii B: scratchpad
   `refusal_stub.mjs` / `refusal_verify.mjs` (session 5ab04918); a stub `/auth/onboarding` MUST
   include `next_action` or the dashboard crashes (stub gap, not a product bug).
2. How the last merges were done (reuse): quiesce = `railway run --service worker <venv python>
   C:/Users/Windows/bl-checks/quiet.py` from the OneDrive repo (linked) plus a read-only check of
   `batch_runs` pending/running and `alembic_version`; merge with `--match-head-commit`; verify
   `railway status --json` shows the merge SHA on api/worker/beat. FE types: `git -C <BE> show
   <sha>:schema/openapi.json > tmp; npx --no-install openapi-typescript tmp -o
   lib/api-types.generated.ts`. FE render proof: scratchpad stub `elig_stub.mjs` +
   `elig_verify.mjs` (session 5ab04918 scratchpad), `.env.local` pointing at the stub, `next build`
   + `next start -p 3111`; delete `.env.local` after.
3. Prod FE verify without a server token: in the owner's tab, read the bearer from
   `/api/auth/session` in-page (as `lib/api.ts` does), call the API in-page, return counts only.

## 2b-ii Phase A — what the PR contains
Files (all on the branch):
- `src/api/config_eligibility.py` (NEW): `config_run_eligibility(db, user, configs, now) ->
  dict[id, ConfigRunEligibility]`, `run_in_flight_message`, `ai_limit_message`, `month_start`,
  `next_month_start`. Codes in gate order: `config_inactive` (PAGE-ONLY; POST /jobs 404s inactive),
  `run_in_flight`, `not_entitled` (only when `ENTITLEMENT_ENFORCEMENT`, which is **true in prod**),
  `ai_limit` (resumes next UTC month), `frozen|ended|over_limit` (2b-i `run_eligibility`). ≤ 6 queries
  per call. Keeps the `Violation` for the gate.
- `src/api/routes/jobs.py`: `enqueue_scrape_job` decides via the evaluator; 409/402 bodies
  byte-identical. The index-backed race path after the flush is unchanged. Unused `CountyConnector`,
  `normalize_plan` imports removed.
- `src/api/routes/scrapers.py`: `_with_run_eligibility` fills `GET /scrapers` and `GET /scrapers/{id}`.
- `src/api/schemas.py`: `ConfigRunEligibilityResponse` (invariant validator) +
  `ScraperConfigResponse.run_eligibility` (null on other routes = "not computed").
- `src/scrapers/registry.py`: `pick_connector(connectors, record_type)` — oldest `(created_at, id)`
  active connector listing the record type; `get_scraper_class` query gains ORDER BY and uses it.
- `tests/test_config_eligibility.py` (34 tests), `schema/openapi.json`, `tasks/todo-run-eligibility-2b-ii.md`
  (plan + Codex rounds + Review).
Latent bug fixed: AI usage judged by unordered `.first()` + county-wide join — manual runs in a mixed
county counted as AI (reproduced on the old gate: 402 "50/50"). Prod: no mixed county, no duplicate
(state, county, record_type) — no live refusal moves.

## Phase B outline (not built; needs its own plan + owner decision)
Still-prose 402s: AI limit (`POST /jobs`), account rule (`POST /jobs`, `POST /batches`). Recommended
NON-breaking: keep `detail` as the same prose, add top-level `code` + `resumes_at` via a custom
exception + handler; declare the body in OpenAPI; FE `readErrorBody` (`lib/api.ts:603`) must read the
top-level code and keep `resumes_at`; FE `toastError` routes every 402 to `toastUpgrade` — a `frozen`
code should say "Update payment". Tests: top-level, nested (entitlement), bare-string legacy bodies.

## Follow-ups queued (Codex P2s declined for Phase A; plan says Phase A moves no refusal)
- `connector_unavailable`: gate + page refuse a run no active connector can serve (today both accept,
  worker fails with UnsupportedCountyError).
- `jobs.was_ai` snapshot (migration) so the monthly AI count doesn't re-classify history when a
  connector changes.
- Open P3s: 2b-i over-limit prose still inline f-strings (BE); FE generated type marks eligibility
  fields optional.

## Failed attempts / landmines hit this session (don't repeat)
- 🛑 **Two-dot diff to Codex after main moved** → main's new PRs looked like reverts → bogus GATE FAIL.
  Always `git fetch` and review `origin/main...HEAD`; rebase first. Main moved 3 times today
  (#366/#368, #370/#372, #373).
- 🛑 **`cmd | tail -1` hides the exit code** — a ruff failure slipped into a commit (fixed in
  `e115cbec`). Run ruff/tsc/eslint WITHOUT a pipe, or check `$?` / `PIPESTATUS`.
- 🛑 **Local Postgres PANIC** ("could not truncate file … Permission denied", Windows file lock) mid
  suite → 176 connection errors that look like test failures. Check `bl-testenv/pg.log`; wait for
  "ready to accept connections"; re-run that part.
- Codex plan reviews sometimes report "not implemented yet" as P1 — say explicitly "this is a PLAN
  review; today's code is shown only as context".
- `.venv-schema` is dead (anaconda gone). `bl-rescat-venv` pins the CI fastapi 0.141.1 / pydantic
  2.13.4 — regenerate `openapi.json` with it, confirm with `--check` + a structural JSON diff vs main.
- Bash heredocs with apostrophes in Python text broke; write longer scripts to scratchpad files.
- Test connectors are not cleaned by the `db` fixture — use unique county names and delete them
  (see the `connectors` fixture in `tests/test_config_eligibility.py`).
- Codex prompts > ~32 KB: pipe via stdin, run from an empty scratch dir:
  `codex exec - -c 'mcp_servers={}' --skip-git-repo-check -s read-only < prompt.txt > out.txt`;
  the verdict is after the LAST `codex` line. Never `codex review` (it runs pytest on your test DB).

## Test environment
- Isolated DB `bridgeleads_eligibility_test` (at migration 104), env
  `source C:/Users/Windows/bl-testenv/env-eligibility.sh` (Redis db 7; sets `$PY`). Never bare pytest.
- Full suite: 8 parts (`ls tests/test_*.py | sort | split -n l/8`), foreground, ≤ 590 s each; split a
  slow part in two. Python `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`, ruff 0.15.6.
- Owner's Chrome tab (claude-in-chrome) was logged in to bridgeleads.io this session; it may have
  expired.
