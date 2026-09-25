# HANDOFF: contact lookup — Phase 1b-1a on PR #356, CI green, NOT merged (2026-09-24)

Read top to bottom before acting. Everything is verified unless marked otherwise.
Previous handoff (1b-0, still useful): `docs/HANDOFF-lookup-contacts-1b0-2026-09-22.md`.

## 1. The goal

Contacts (phone / email) can only be bought as a side effect of a scrape. To trace 38
already-delivered leads the owner had to build a SECOND scraper and re-scrape Pierce. The
agreed direction:

1. **"Look up contacts" as a first-class action** on leads that already exist: pick a run or a
   tab, see a cost quote, confirm, lookups are bought. No re-scrape.
2. **Fix answer reuse** so it can never hand one owner's contacts to a different owner at the
   same address. **Phase 1a. DONE, merged, live since 2026-09-20.**

## 2. WHERE YOU ARE

| | |
|---|---|
| Branch | `feat/lookup-1b1-schema` in `C:/Users/Windows/bl-wt-lookup` |
| PR | **#356**, 5 commits ahead of `origin/main` |
| HEAD | `7bc1893a` |
| CI | **GREEN on `7bc1893a`** — Test 14m33s, Dependency Audit 40s, Detect changes 7s |
| PR state | `MERGEABLE` but **`BEHIND`** — 2 commits behind main, protection is `strict: true` |
| Migration | **101** (schema only) |
| Tests | 69 green locally (25 schema + 44 claim/over-quota); ruff clean |
| Codex | 3 diff-review rounds so far; **round 18's fixes are UNREVIEWED** |
| Merged | **No.** No Tracerfy credits spent. Production untouched by 1b-1a. |

**Already merged and LIVE in production (do not redo):**
- Phase 1a — migration 098, subject-keyed reuse. Cut over 2026-09-20.
- Phase 1b-0 — migration **100**, one active skip-trace claim per lead. Merged `0074196`,
  **applied in production** and verified by the objects.
- **PR #357 `25a04eaf`** — CI cost controls (see §7).

## 3. What 1b-1a does (PR #356)

Schema only. No endpoint, no writer, no frontend.

- **Three tables**: `contact_lookup_actions`, `contact_lookup_action_results`,
  `contact_lookup_action_events`, with RLS, role-guarded grants and tenant-carrying
  **composite** FKs so a child row can never point at another account's action or lead.
- **`UNIQUE (id, user_id)` on `jobs`, `results`, `contact_lookup_actions`** — built
  CONCURRENTLY, then promoted with `ADD CONSTRAINT ... USING INDEX`. Only `scraper_batches`
  had one; finding 15-9 named `results` alone and missed `jobs`, so the FK it specified could
  not have been created at all.
- **`results.last_trace_outcome`** — COLUMN ONLY, nullable, no default, no writer.
  🛑 **NULL means UNKNOWN, never `provider_rejected`.** `tracerfy_ingest` writes
  `skip_trace_status='errored'` for provider-accepted-but-unmatched work, which is BILLABLE.
  Its eight live writers land in 1b-2.
- **`pending_skip_trace_rows.action_id`** — nullable + indexed. 1b-0 removed it as dead code;
  it is not. A Tracerfy batch spans tenants and actions and `SkipTraceQueue` keeps only the
  FIRST row's metadata, so ingest rebuilds attribution from the pending rows.
- **Two triggers.** Grants bound WHICH TABLE, RLS bounds WHICH ROWS; neither can express
  "create an initial verdict, never transition one". Both key on the tenant GUC: the API
  always sets `app.current_user_id`, the worker's `system_sync_session()` never does.

🛑 **CARRIED INTO 1b-2:** a worker using `rls_sync_session()` sets the GUC and **will be
refused by both triggers exactly as the API is**. The action worker must use
`system_sync_session()`. It fails closed, which is right, but reads as a baffling permission
error if this is not read first.

## 4. Active files

```
NEW  alembic/versions/101_contact_lookup_action_schema.py   643 lines
NEW  tests/test_contact_lookup_schema.py                    465 lines, 25 tests
     src/db/models.py                       3 model classes + 2 columns + 2 composite uniques
     scripts/provision_rls_roles.sql        grants + REVOKEs + $verify$ allowlist
     scripts/apply_rls_cutover_policies.sql role-targeted _app/_system policies
     scripts/apply_rls_force.sql            the tbls[] array (or FORCE never applies)
     scripts/_cutover_step2_grants_policies.py   the FOURTH script (see §6)
     tasks/todo-lookup-contacts.md          THE PLAN — round 16 + findings 16-1..16-12
```

**Read `tasks/todo-lookup-contacts.md` in full before writing code.** Findings 15-1..15-17 and
16-1..16-12 each exist because a round found a way to double-charge a customer or strand a row.

## 5. How to run things

- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python` — 🛑 the bare `python` on PATH is a
  DEAD anaconda install; a patch script "succeeded" silently against it once.
- **NEVER bare `pytest`** (repo `.env` is PRODUCTION):
  `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` then
  `"$PY" -m pytest -m "not integration" -q -p no:cacheprovider -o addopts="" <files>`
- 🛑 **The test DB schema comes from `alembic upgrade head`, NOT `create_all`.** Nothing in
  this repo calls `create_all`, whatever the comments in `models.py`, `alembic/env.py` and
  migrations 049/089 say. If the DB is missing, create it then run alembic.
- 🛑 Always wrap pytest in `timeout 420` — see the 78-minute incident in §6.
- Codex: `codex exec - -C <repo> -s read-only -c 'model_reasoning_effort="high"'
  -c 'mcp_servers={}' -c 'web_search="cached"' -c 'experimental_use_skills=false' < prompt.txt`
  🛑 Write the prompt with the **file tool**, not a bash heredoc — two heredocs died on shell
  quoting and silently wrote nothing.
- Prod read-only: `railway run --service worker <python> <script>` from the OneDrive checkout.
  Scripts that only use `DATABASE_URL` + psycopg2 work; do not import `src.*` (stale checkout).

## 6. FAILED ATTEMPTS AND LANDMINES — do not repeat these

**THE BIG ONE: two of the six P1s on this PR were introduced BY MY OWN FIXES.** A fix is a
change, and it deserves the same review as the code it fixes.

**`REVOKE UPDATE ON t FROM r` ALSO WIPES COLUMN-LEVEL GRANTS.** I added a column-level
`GRANT UPDATE (status, ...)` and put the table-wide REVOKE later in the file, and asserted in
the commit message that table revokes leave column grants alone. They do not. Measured:
`GRANT UPDATE (a,b)` → `['a','b']`; `REVOKE UPDATE ON t` → `[]`. The API would have had **no**
update privilege and could not have dispatched an action. **The revoke must precede the
column grant.**

**A FOURTH RLS SCRIPT.** `scripts/_cutover_step2_grants_policies.py` carries its own grant
list including `GRANT ... UPDATE ON ALL TABLES` to the system role. Third time in this project
that a script in `scripts/` turned out to be part of a design it was never listed in (1a's
subject key, 1b-0's queue concurrency, now the grant surface). **The RLS family is FOUR files.**

**THE MIGRATION WAS NOT REPLAY-SAFE, AND MY TEST PROVED THE WRONG THING.**
`autocommit_block()` COMMITS the transaction it is entered from, so the columns and CHECK were
durable before the index build while `alembic_version` was not written. A lock timeout on the
attach left a half-applied database, and the replay aborted on "column already exists". I had
"tested replay" by replaying the two index HELPERS, which were already idempotent — the defect
was in the statements before them. Reconstruct the REAL half-applied state to test this.

**A CLEAN SECURITY/REVIEW PASS IS EVIDENCE ABOUT THE QUESTIONS ASKED.** Rounds 17 and 18 each
found P1s that round 16 structurally could not, because the question changed.

**A TEST THAT COST 78 MINUTES.** An append-only test asserted UPDATE *and* DELETE in one loop
and reused the session after each failed statement. The DELETE blocked on a row lock and the
reuse raised `MissingGreenlet` instead of the database error. A 13-second file became 4,708
seconds. **One forbidden statement per test; never reuse a session after it raises.**

**FOUR STALE COMMENTS AGREEING WITH EACH OTHER READ AS DOCUMENTATION.** `models.py`,
`alembic/env.py` and migrations 049/089 all claim `create_all` builds the test schema. It does
not. This misled a pattern extraction, a Codex finding (16-12, withdrawn) and me.

**`str.replace(x, y, 1)` HIT THE WRONG OCCURRENCE.** An edit removed an import from a
different test than intended. Anchor on unique surrounding text.

**GitHub Actions was billing-blocked mid-session.** `fail` in 2-4s with empty steps = billing,
not code. Resolved by the owner; PR #357 now limits the burn.

## 7. What PR #357 changed about CI (merged, live)

CI is metered on this PRIVATE repo ($0.008/min, Test ≈ 14 min). A $4 top-up lasted under a week.
- **`concurrency` + `cancel-in-progress`** — a new push to a PR cancels the superseded run.
  **PR-ONLY**: a push to main is a DEPLOY (Railway redeploys, `start.sh` migrates on boot), so
  cancelling it could interrupt a migration.
- **Docs-only PRs skip the 14-minute suite** via a ~9s `changes` gate job. **NOT `paths-ignore`**:
  `Test` and `Dependency Audit` are REQUIRED checks, and a workflow that never runs never
  reports them, so the PR would hang on "Expected — waiting for status" forever.
- 🛑 **UNPROVEN:** that a *skipped* required check satisfies branch protection. The first
  docs-only PR after this proves it. If it hangs, revert #357.

## 8. OPEN DECISIONS FOR THE OWNER

1. **Deploy style for migration 101.** Codex says **QUIESCED**. 🛑 **In this repo merging IS
   deploying** — push to main redeploys Railway and `start.sh` runs `scripts/migrate.py` on
   boot. There is no step in between, so quiescing is a PRE-MERGE action. Migration 100 shipped
   unquiesced for exactly this reason. 101 takes a brief ACCESS EXCLUSIVE lock on `jobs` and
   `results` (bounded `lock_timeout = 5s`), and it IS backward-safe (code can roll back with
   101 applied — 100 was not).
2. **Merge #356?** Not merged.

## 9. NEXT STEP (exactly where to resume)

1. **Run ONE Codex pass on `7bc1893a`.** Round 18's fixes — the grant ordering, the fourth
   script, the tightened event guard — have NOT been reviewed. Rounds 17 and 18 each returned
   NO-GO, so do not assume this one is clean.
2. **Rebase onto `origin/main`** (2 behind; `strict: true` requires up to date). That triggers
   one more CI run.
3. **Ask the owner §8.** Do not merge without an answer.
4. **Then Phase 1b-1b — the HARD per-account dispatch cap.** Owner reversed D3 (now **D3-b**):
   the cap moves into 1b-1 and lands **BEFORE the quote**, because the quote promises a pause
   reason and resume time the cap must actually compute. It is NOT a read-path feature — it
   changes the component that SPENDS. Required shape (finding 16-4, 16-5):
   - **Reserve capacity inside the SAME transaction that moves rows `queued -> submitting`**,
     not as a pre-check beside the existing global cap — that check runs in its own session
     BEFORE the claim's advisory lock, so two ticks can both pass it (15-11, the cap is SOFT).
   - Effective allowance `min(global_remaining, account_remaining)`; count `submitting` rows of
     unknown provider outcome as spent until reconciled.
   - **No database lock held across the Tracerfy call.**
   - **Fair selection.** `ORDER BY enqueued_at LIMIT 5000` plus an account filter starves
     tenants. Round-robin over eligible `user_id`s, and TEST one big-backlog tenant against
     several later ones.
   - Per-account resume time as ONE window query, published via a Redis pipeline; Redis is
     ADVISORY.
5. **Then 1b-1c** (planner + quote endpoint), **then 1b-2** (writers, `last_trace_outcome`,
   the reconciler).

**🛑 CONSULT CODEX ON THE 1b-1b PLAN BEFORE WRITING CODE.** Not the same as reviewing the diff
afterwards: the pre-code consult on 1b found nine P1s, and on 1b-1 another seven, that diff
review would never have reached.

## 10. Housekeeping NOT done

- **No BUILD_JOURNAL entry for 1b-1a.** The latest entry covers 1b-0.
- **The plan file is behind.** `tasks/todo-lookup-contacts.md` has round 16 and the 16-12
  withdrawal, but the three Codex DIFF-review rounds since (six P1s) live only in commit
  messages.
- **13 open PRs**, five Dependabot. 🛑 `stripe` 11→15 and `redis` ≥6.5 (breaks Celery via
  kombu) are recorded landmines — do not merge as-is.
- Deleting the now-inert legacy skip-trace cache rows: PII hygiene, separate ops step.
- **The D2 gap:** an advanced trace costs 2 provider credits but bills 1 row.

## 11. Session artifacts

Scratchpad: `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/5602769c-5697-48d8-aea9-d012c4107911/scratchpad/`
- `codex_1b1_consult{,_out}.txt` — the pre-code plan consult (round 16, seven P1s)
- `codex_1b1a_review_out.txt` — round 17 (three P1s)
- `codex_p1fix_review_out.txt` — round 18 (two P1s + a P2)
- `codex_postrebase_out.txt`, `codex_final2_out.txt` — the 1b-0 post-rebase rounds
- `prod_check_100.py`, `prod_running_jobs.py` — read-only production checks
