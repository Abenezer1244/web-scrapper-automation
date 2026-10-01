# HANDOFF: contact lookup Phase 1b-2 (the write path), 2026-09-30 (end of session)

Read this whole file first. Then read these sections of `tasks/todo-lookup-contacts.md`
(search the headings). Together they are the NORMATIVE contract:
1. **"## Phase 1b-2 — the WRITE path"**: Facts, Design (S1-S3), and the five steps
   2a/2b/2c/2d/2e.
2. Every Codex consult subsection under it:
   - **r1 → V1-V8**;
   - **r2 → W1-W7**;
   - **r3 → X1-X3**;
   - r4/r5 **PLAN: GO**;
   - **OWNER DECISIONS (2026-09-30)**.

   Where these amend the step text, the amendment wins.
3. The "BUILT / MERGED" records for 2-0, 2a-i and 2a-ii (what exists now, and how it was
   verified).
4. Background, if you need it: "Phase 1b - the action, backend" (findings 15-1 to 15-17),
   "Phase 1b-1 — REVISED by Codex round 16" (16-x, 19-x, 20-3), and "Carried into 1b-2".

## The goal
Phase 1 of contact lookup: a customer buys skip-trace lookups for the leads on a results tab.
The provider is Tracerfy, and the operator pays per credit (normal 1, advanced 2).
- **1b-1 (LIVE):** the ledger schema (101), the hard per-account spend cap, the pause state,
  the planner, and `POST /jobs/{id}/contact-lookups/quote`.
- **1b-2 (THIS PHASE):** the write path. A customer confirms a quote, a durable action row is
  created, a worker claims the quoted leads into the paid queue, and a reconciler
  re-drives / expires / settles actions.
- The design adds NO new spend path. Leads enter the queue only through
  `lock_job_for_claim()` + `claim_skip_trace_rows()` (H3). From there the existing
  dispatcher, caps, ingest and metered billing do the rest.
- **Settlement is bookkeeping, never billing.** Billing stays per pending row at ingest.

## Where things are
| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup`. **NEVER the OneDrive checkout: its `.env` is PRODUCTION.** |
| Branch to continue on | **`feat/lookup-1b2b-worker`**, off main `11069d6a`. LOCAL; it holds only this handoff. Build 2b here. |
| `main` | `11069d6a` (#404, another session's AI-mode removal). 1b-2 2-0 / 2a-i / 2a-ii are live. |
| Test env | `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY`), then `export DEBUG=false`. Local PG16 `bridgeleads_lookup1b_test` is **at rev 107**. Local Redis 5.0.14 db 13. |
| Store-failure test | set `BL_TEST_REDIS_SERVER=C:/Users/Windows/bl-testenv/redis/redis-server.exe` when running `tests/test_contact_lookup_quote.py`. |
| Local migrate | `$PY scripts/migrate.py` with the env sourced. alembic's env.py pins the target to `TEST_DATABASE_URL_SYNC`; `$PY -m alembic downgrade/stamp` also work there. |
| OpenAPI | `C:/Users/Windows/bl-schema-venv/Scripts/python scripts/export_openapi.py --check`, with the test env sourced (settings need the vars). PyJWT there was bumped by hand to 2.14; main is now 2.15. |
| Prod checks | From the OneDrive dir: `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py`. A migration object check is in this session's scratchpad (`verify_107.py`) and is read-only. |
| Scratchpad (this session) | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/49da3c50-c8e3-4dae-b468-3620a70ca090/scratchpad/`: `codex_*.txt` prompts + `_out.txt`, `mut20.py`, `mut2a.py`, `mut2aii.py` (mutation runners: the pattern to copy), `verify_107.py` |

## State: LIVE vs not
**LIVE**, each merged under the gate and its deploy verified:

| PR | Merge | What |
|---|---|---|
| #386 | `29172543` | `lookup_quote` rate zone + the planner's trial `credit_cap` |
| #393 | `677a6a40` | The quote endpoint |
| #395 | | Journal entry for 1b-1c |
| #398 | `d68563ce` | HOTFIX for my #393 circular import (see "Failed attempts") |
| #400 | `2b907bc1` | The 1b-2 plan + **2-0**: the repair script serialises with the claim and never touches an action's lead; the dispatcher's comments now say cancellation is pending-row-only |
| #402 | `e9397f0f` | **2a-i**, migration **107** |
| #406 | `8c9afb98` | **2a-ii** |

What #402 (2a-i, migration 107) added:
- `pending_skip_trace_rows (action_id, user_id)` FK → `contact_lookup_actions (id, user_id)`,
  ON DELETE NO ACTION;
- the `unmatched_unbilled` disposition;
- `contact_lookup_actions.quote_snapshot JSONB NOT NULL DEFAULT '{}'`.

It was verified IN PRODUCTION BY THE OBJECTS.

What #406 (2a-ii) added:
- `claim_skip_trace_rows(..., action_id=None)`: writes `action_id`, and JOINs the action on
  id + tenant + job (W6);
- `report["held_ids"]` (W5);
- with `action_id=None`, the scrape path's SQL is byte-identical (pinned by a golden test).

**NOT STARTED:**
- **2b** worker;
- **2c** reconciler;
- **O-C** the billing fix (`rows_sent`), **a HARD GATE before 2d**;
- **2d** the confirm endpoint (the switch that makes it live);
- **2e** the GET status endpoint.

## Owner decisions (2026-09-30)
- The plan is APPROVED; build in order: 2-0 → 2a → 2b → 2c → O-C → 2d → 2e.
- O-A: 2d goes live with its merge (no feature flag).
- O-B: kill switch off when the worker runs → the action WAITS, and the reconciler re-drives
  it until a 30-min deadline, then it expires (nothing bought).
- O-C: persist `skip_trace_queues.rows_sent`, compare `rows_uploaded` with it in billing's
  `accepted_all`, and bill `completed` only on a mismatch or a NULL. Its own PR, BEFORE 2d.
- Standing rule (memory `feedback_standing_merge_ok_lookup_queue`): you may merge your own
  PRs in this queue when ALL of these hold:
  - `quiet.py` exits 0 with all four counts at 0;
  - CI is green on the EXACT head;
  - Codex says GO;
  - main is unchanged since the rebase;
  - you merge with `gh pr merge N --merge --match-head-commit <sha>`.

  Never `--admin`.
- **Ask the owner before starting each new phase** if unsure. The owner approved continuing
  through the sequence, and the previous session paused before 2b to start it fresh.

## Next step: 2b, the worker (branch `feat/lookup-1b2b-worker`)
The spec is the plan's "1b-2b" step, AS AMENDED by:
- V1: the worker fails the action closed if the durable `quoted` set ≠ `quoted_count`, and
  re-checks job deliverability under the lock;
- V8 / W7: ONE transition matrix (`ACTION_TRANSITIONS`, `VERDICT_TRANSITIONS`, `_move()`)
  that the 2c reconciler will IMPORT from this module;
- W1: the lease, transaction by transaction.

**The W1 transaction shape:**
- **T1:** the start CAS `dispatching→running` (lease token, `lease_expires_at` = now + 10 min,
  `started_at`, an event) COMMITS alone.
- **T2:**
  1. `SELECT … FOR UPDATE` the action;
  2. re-check `running` + `lease_token` + not expired;
  3. `lock_job_for_claim`;
  4. read the quoted set:
     - `results JOIN contact_lookup_action_results ON disposition='quoted'`;
     - scoped to the action's `user_id` + `job_id`;
     - ordered `(created_at, id)`;
  5. classify each lead in order:
     - `classify(row, current policy)`: a stricter current policy only EXCLUDES;
     - charged-unanswered → `already_answered`;
     - cache hit → ORM copy (encrypted columns: never raw SQL) → `reused`;
     - else a claim payload;
  6. `claim_skip_trace_rows(db, payloads, action_id=…, report=…)`:
     - returned ids → `newly_queued`;
     - `report["held_ids"]` → `ineligible`, with a per-lead event, reason `trial_allowance`;
     - a lost race → a verdict from the current status;
  7. every quoted lead gets exactly ONE verdict; counts are aggregated from the verdicts;
     status `claimed`, `claimed_at`, the lease CLEARED (15-15), an event;
  8. ONE commit.
- Every fail / wait / abandon transition is its own committed transaction.

**Gates, before T2's claim:**
- the job is `done` AND delivered (`_job_delivered_sql`, 15-6), else `failed` + all
  `abandoned`;
- the plan is in `SKIP_TRACE_ADDON_PLANS`, and `paid_lookup_access` is not blocked, else
  `failed` / `abandoned`;
- the kill switch or token is off → back to `dispatching`, lease cleared (O-B).

**Shared code, not a copy:**
- Extract the enqueue's cache-hit copy and charged-unanswered rule from `enrich.py` into
  helpers both paths call (consult r1 Q4: extraction over copy + parity).
- `enrich.py` is LIVE paid code, and #399 just changed it (`held_lookup_message`, `report=`).
  Read the current version.

**Files (5):**
1. NEW `src/workers/contact_lookup_action.py`;
2. `src/workers/__init__.py`: add it to Celery `include`. `tests/test_import_cycles.py`
   then covers it automatically;
3. `src/workers/tasks_helpers/enrich.py` (the extraction);
4. NEW `tests/test_contact_lookup_action.py`;
5. `tasks/todo-lookup-contacts.md`.

This handoff rides outside the 5, per the #376 precedent.

**The worker is idle until 2d.** Nothing dispatches to it yet, so the 2b merge changes nothing
live. The enrich extraction DOES touch the live enqueue: regression-test it hard.

**Tests the plan requires** (real PG + Redis; no mocks; a pass-through spy is OK):
- redelivery and double delivery → one set of pending rows;
- the action racing a scrape enqueue on the same lead → exactly one row, nothing stranded;
- a lead answered or queued after the quote → not bought;
- a non-quoted id or an other-tenant id can never be bought;
- the trial cap holds, with held leads recorded;
- kill switch → the action waits;
- job not done → abandoned;
- every verdict is written;
- the lease is cleared at claim;
- a kill between T1 and T2 is recoverable (the reconciler arrives in 2c);
- parity: what the action claims == what the enqueue would claim for the same leads;
- the transition matrix enforced;
- mutations.

**Workflow per PR** (binding):
1. Codex pre-code consult on anything not already in the plan.
2. Build.
3. Real-DB tests + a mutation runner.
4. Regression over every test file that touches pending rows / claim / ledger. Find them with
   `grep -rlE "pending_skip_trace_rows|PendingSkipTraceRow|contact_lookup_action|claim_skip_trace_rows" tests/*.py`
   and run them in 2 chunks, each < ~8 min, output to a file.
5. ruff.
6. Codex THREE-DOT diff review until GO.
7. Push, open the PR, wait for CI.
8. Rebase + Codex re-check if main moved.
9. The merge gate, then verify the deploy.

## Parallel session (coordinate merges)
- Another local session (`web-scrapper-automation-30`, security / AI-mode work) merges to the
  same main. Message it via SendMessage before a merge slot.
- The protocol: ask it to hold merges during your ~22-min CI window. Whoever goes green first
  merges, and the other rebases.
- It has docs PR **#408** pending, and will ask before merging.
- It removed AI mode: `src.scrapers.ai`, `ai_assessor`, `settings.AI_*`, `ANTHROPIC_API_KEY`
  are GONE. PACS fallback URLs are now in `src/scrapers/enrichment/assessor_urls.py`.

## Failed attempts / traps hit this session (don't repeat)
- **I shipped a circular import (#393) that broke every ops script.** It took days to notice:
  the api and worker survive by import order.
  - The chain: `src.scrapers` → `base_scraper` → `src.api` → routers → planner →
    `pierce_atip_owner` → `base_scraper`.
  - In `src/api/routes/*`, import scraper-reaching modules INSIDE the handler.
  - `tests/test_import_cycles.py` (fresh interpreters) pins it.
  - Memory: `landmine_router_importing_scrapers_breaks_scripts`.
- **A test hardcoded a local `redis-server.exe` path → RED in CI.** CI's Redis is a
  `redis:7-alpine` container with ACLs. Memory: `landmine_ci_has_no_local_binaries`.
- **main moved under a PR 3-5 times in one day.** A clean rebase is NOT a fact check: #390
  changed `_enqueue_skip_trace_rows` (an `attempt_token` fence). Read main's diff against the
  plan's facts every time.
- **The required Dependency Audit fails every PR on a new PyJWT CVE.** That happened twice
  (#391 → 2.14, #407 → 2.15). Not your diff. The parallel session owns the bumps; don't
  duplicate them.
- **Local test DB drift:** #394 added migration 106 and local tests failed with
  `UndefinedColumn`. After any migration lands on main, run `$PY scripts/migrate.py` locally.
- **Raw `text()` queries return `uuid.UUID`, not str.** Compare with `str(...)`. Postgres
  `"char"` (e.g. `confdeltype`) comes back as BYTES from asyncpg: `::text` it.
- **Heredoc + `\n` escaping in inline Python broke twice.** Write mutation runners with the
  Write tool. Files are CRLF in the working copy (LF in the index): edit with the Edit tool, or
  with byte-level Python using the file's own newline.
- **`sleep N` alone is blocked by the harness.** Wait with an `until <check>; do sleep; done`
  loop, or `run_in_background`. Background watchers can be killed for low memory; if so,
  re-check once instead of restarting.
- **Prod `alembic_version` reads EMPTY from the worker role.** Verify migrations BY THE
  OBJECTS (`pg_constraint`, `information_schema`), never by the version table.
- **Codex findings that were real this session** (the review loop works; keep it):
  - lock order (pending, then results), or a dispatcher tick can deadlock;
  - an unguarded `party_name` on the parcel recovery;
  - a lease committed separately from the claim (W1);
  - billing's `accepted_all` compares uploads with STAMPED rows, not SENT ones → O-C;
  - a test asserting only "absent" instead of the exact SQL.
- An **equivalent mutant** is fine if argued and recorded: the claim join's tenant check is
  implied by its job check + 101's FK.

## After 2b
- 2c: the reconciler. `src/workers/scheduler_helpers/contact_lookups.py` + a `scheduler.py`
  entry (120 s). Extract `queue_accepted_all` from `skip_trace_usage.py`, shared by billing
  and the reconciler. It derives terminal verdicts from pending rows by `action_id` (S1). The
  `provider_reconciliation_required` reason. Per-action cursor; beware
  ORDER BY oldest + LIMIT starvation.
- Then O-C, then 2d (confirm), then 2e.
- A BUILD_JOURNAL entry for 2-0/2a/2b+ (ask the owner where it lands), then 1c (frontend).
