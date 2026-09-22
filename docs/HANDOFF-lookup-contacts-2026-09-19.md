# HANDOFF: contact lookup action + reuse correctness (2026-09-19)

Read this top to bottom before acting. Everything here is verified unless marked otherwise.
Previous handoff (now closed): `docs/HANDOFF-skip-trace-followups-2026-09-19.md`.

## 1. The goal
Contacts (phone / email) can only be bought today as a side effect of a scrape. The owner had to
build a SECOND scraper with a custom date range and re-scrape Pierce just to buy contacts for 38
leads an earlier run had already delivered. The agreed direction (owner approved) is:

1. **"Look up contacts" as a first-class action** on leads that already exist: pick a run or a tab,
   see a cost quote, confirm, and the lookups are bought. No re-scrape, no second scraper.
2. **Fix answer reuse** so it can never hand one owner's contacts to a different owner at the same
   address. Reuse is keyed on the address with NO owner name, and probate makes the collision
   likely: the deceased owner first, an heir later.

Longer term (Codex design review, agreed but NOT started): a lookup ledger (Phase 2) and
lead-level contacts so a contact belongs to the lead rather than to one run's row (Phase 3).

## 2. What is DONE and LIVE in production (this session, do not redo)
- **BE #344** merged `5bd9c59`, deployed. `results.skip_trace_source` (migration 097) records
  whether an answer was a new `lookup` or `reused`. Verified live read-only: column present, CHECK
  validated, all 171,460 existing rows NULL as designed.
- **FE #157** merged `6c435d0`, deployed: "N came from an earlier lookup, so no new lookup was
  bought."
- **BE #345** merged `3abe087`: journal entry + closed the previous handoff.
- **Owner's 38 leads: SOLVED and verified in prod.** The owner ran "rerun" (`61ea0717`, Pierce
  probate, custom Aug 18 to Sep 17, skip trace on). 46 rows, 0 new, 46 already delivered,
  `billed_count` 0. All 44 frozen leads found: **37 paid lookups (30 hit, 7 miss)** = exactly the
  pre-check's maximum, 1 reused, 6 no-address. 2 extra leads reused at no charge. The original run's
  rows stay `not_attempted` by design (a run's rows settle once).
  Checker: `<scratchpad>/prod_compare.py` (read-only, counts only, `railway run`).

## 3. What is OPEN
| What | Where | State |
|---|---|---|
| Phase 1 plan (this work) | BE `C:/Users/Windows/bl-wt-lookup` (`feat/lookup-contacts-action`, `263cfa9`) | plan committed, NO code yet |
| Phase 1 FE worktree | `C:/Users/Windows/bl-wt-lookup-fe` (same branch name, off master `6c435d0`) | empty, nothing done |
| FE security upgrade | `C:/Users/Windows/bl-wt-deps-fe` (`chore/security-deps-2026-09-18`, `276045e`) | **LOCAL ONLY, never pushed, owner has not given the go** |

⚠️ `origin/main` has moved to `6b03ece` (#346, Snohomish assessor roll) since this branch was cut
from `3abe087`. Rebase before opening a PR.

### The plan: `tasks/todo-lookup-contacts.md` (386 lines, on this branch)
It carries the verified facts it rests on (with file:line), the three owner decisions, the phase
breakdown and the test matrix. **Read it in full before writing code.** Phases:
- **1a reuse correctness** (no migration): a versioned `lookup_subject_key` including trace type and
  the exact names sent to Tracerfy, switched across FIVE call paths at once, legacy address-only
  keys no longer read, cutover under the existing kill switch.
- **1b the action, backend**: migration 098 (3 new tables + `pending_skip_trace_rows.action_id` +
  partial unique index), a pure planner shared by quote and worker, quote + confirm endpoints, the
  worker, a reconciler, an action status endpoint.
- **1c the action, frontend**: button per tab, confirm dialog with the cost, progress from the
  action status endpoint.

### Codex: 13 rounds, GATE PASS on round 13 for starting Phase 1a only
Prompts and outputs: `<scratchpad>/codex_p1plan*.txt` and `*_out.txt`; the design consult is
`codex_design.txt` / `_out.txt`. Rounds 1-12 all FAILED, and each failure changed the design. The
findings that matter most, because re-deriving them costs hours:
- The quote MUST be bound to a durable set of lead ids written to the DB at confirm time. Task
  arguments cannot bound the worker, and a lead that was in progress at quote time must not be
  buyable later.
- Claims must be lease-fenced: a stalled worker that wakes after the reconciler gave up must commit
  nothing.
- A release back to `not_attempted` may only touch rows still locally `queued` with no
  `tracerfy_queue_id` and no `submitted_at`. Anything that reached Tracerfy may have been charged.
- Every state change commits with its event row in ONE transaction, or the audit can disagree with
  the queue.
- Round 13's PASS is conditional on four tests at diff time (legacy-only cache hit reused by none of
  the five paths; advanced hashes null names; pre-cutover pending rows processed with v2; dedup and
  charged-unanswered use the same helper) plus a counter for v2 key usage.

### Three owner decisions still open (recommendations in the plan)
- **D1** address-only (advanced) answers: reuse per address and document that owner isolation does
  not apply, or never reuse them.
- **D2** advanced costs Tracerfy 2 credits but bills the customer 1 lookup: show the customer-billed
  number and log the margin gap separately.
- **D3** the daily cap is GLOBAL across tenants and pauses silently: surface it end to end.

## 4. NEXT STEP (exactly where to resume)
1. Ask the owner for D1/D2/D3 if they have not answered, then **start Phase 1a**. It is
   self-contained and ships on its own. Nothing else in Phase 1 should start before 1a is merged.
2. Before the first line of code: rebase `feat/lookup-contacts-action` onto `origin/main`
   (`6b03ece`+), and re-read `tasks/todo-lookup-contacts.md`.
3. Phase 1a order: the key helper + its normalization tests first, then the five call paths
   together, then the cutover runbook. Run the isolated test DB (see §6), never bare pytest.
4. Codex reviews the 1a diff before any PR (`codex` skill, review mode), then the security Master
   Review (§14 of the pack). Any P1/High = NO-GO.
5. Separately, waiting on the owner's word only: push `chore/security-deps-2026-09-18` and open its
   PR. It fixes a CRITICAL `next` RCE that is live in production today.

## 5. Failed attempts / dead ends (do not repeat)
- **My own Phase 1 plan failed Codex 12 times.** Do not "simplify" the concurrency design back:
  every piece of it (durable quoted set, lease fencing, single-transaction claim, conditional
  release, reconciler) exists because a specific round found a way to double-charge a customer or
  strand a row. The plan says which.
- The previous session concluded the login bug was "Next 16.3 RSC navigation or the middleware".
  Wrong. It was OUR mount guard never re-armed (`app/(auth)/login/page.tsx`), dev-only, and a
  production build was never affected. Isolated by testing next 16.3.5 with the OLD next-auth.
- Do not claim a migration is applied by reading `alembic_version` in prod: the app role sees it
  EMPTY. Prove it by the objects (column, validated constraint).
- The FE type-drift gate reads BE `main` through raw.githubusercontent, which served the pre-merge
  schema for minutes after the merge. The uncached contents API showed the truth; a CI re-run passed.
- Python patch scripts: write them with the Write tool and run them. Nested shell heredocs mangled
  `\n` into real newlines twice this session. Match on regex anchors, not exact indented blocks,
  because earlier edits rewrap the paragraph.
- `npm ci` fails with EPERM while `next dev` is running: stop the server (and its child) first.
- Two background servers plus a build exhausted memory and the harness killed the servers. Run one
  server at a time.

## 6. How to run things (Windows box)
- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python` (Anaconda is gone).
- NEVER bare `pytest` (the repo `.env` is PRODUCTION). Isolated DB, then:
  `TEST_DATABASE_URL=postgresql+asyncpg://bridgeleads:testpassword@127.0.0.1:5432/bridgeleads_prov_test`
  + `TEST_DATABASE_URL_SYNC` (psycopg2, same DB), `DATABASE_URL(_SYNC)` = those,
  `REDIS_URL=redis://127.0.0.1:6379/12`, `SECRET_KEY=<32+>`, `STRIPE_SECRET_KEY=sk_test_fake`,
  `ENVIRONMENT=test`, `alembic upgrade head`, then
  `python -m pytest -m "not integration" -q -p no:cacheprovider -o addopts="" <files>`.
- Local full-stack rig (used for every login/auth check this session): env in
  `<old scratchpad ff73458d>/pe2e_env.sh` (DB `bridgeleads_enrich_e2e_test`, redis 14), API
  `python -m uvicorn main:app --host 127.0.0.1 --port 8011`, FE `.env.local` (throwaway, delete
  after) with `NEXT_PUBLIC_API_URL=http://127.0.0.1:8011`, `AUTH_SECRET`/`NEXTAUTH_SECRET` (32+),
  `AUTH_URL=http://127.0.0.1:3311`, `AUTH_TRUST_HOST=true`, then `npx next dev -H 127.0.0.1 -p 3311`
  or `next build && next start`. Seeded user `enrich.e2e@bridgeleads-local.com` / `E2eLocal!2026`.
  Playwright scripts in this session's scratchpad: `login_probe.py` (logs every navigation and RSC
  fetch), `auth_regress.py` (8 checks), `register_probe.py`, `zod_probe.py`, `verify_email_e2e.py`
  (verified signup end to end: set `EMAIL_VERIFICATION_ENABLED=true` on the local API and mint the
  link token with `mint_verify_token.py`).
- Codex (Windows): `codex exec -c 'model_reasoning_effort="high"' -c 'mcp_servers={}'
  --skip-git-repo-check - < prompt.txt`. It logged OUT mid-session (401 "Missing bearer"); check
  `codex login status` and ask the owner to run `codex login`.
- Prod read-only: `railway run <python> <script>` from the OneDrive checkout. Read-only
  transactions, counts only, no PII in output.

## 7. Session artifacts
This session's scratchpad:
`C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/7dc3a73a-316c-49c8-8e10-73b680d7d3f1/scratchpad/`
- `codex_design.txt` / `_out.txt`: the design review (current design pros and cons, target design).
- `codex_p1plan.txt` ... `codex_p1plan13.txt` + `_out.txt`: all 13 plan rounds.
- `prod_compare.py`: the read-only prod checker for the "rerun" verification.
- `prod_mig_check.py`: read-only proof that migration 097 is live.
- Playwright probes listed in §6; `login_prod_fixed.png`, `register_step3.png` and other shots.
Previous session's scratchpad (`ff73458d-...`) still holds `pe2e_env.sh`, `prod_precheck.py` and
`prod_frozen_d3298b54-...json` (the frozen 44-row snapshot).
