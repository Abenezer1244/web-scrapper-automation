# HANDOFF: contact lookup, after 2d (LIVE) → Phase 1b-2e, the status endpoint (2026-10-02)

Read this whole file first. Then, in `tasks/todo-lookup-contacts.md` (search the headings):
- **"### 1b-2e — `GET /jobs/{job_id}/contact-lookups/{action_id}`"** (line ~3220): the
  original 2e stub. It is three lines and needs a full spec before any code.
- **"## Phase 1b-2d — the confirm endpoint"** and **"### 2d BUILT"** (~4851-5170): the
  contract 2e reads back, and every review round.
- For background, if needed: "## Phase 1b-2 — the WRITE path" (V1, V6, S1/15-5 settlement),
  "## Phase 1b-2c — the reconciler" (P1-P3, AA1 deadline), "## Phase 1c - the action,
  frontend" (~5173; what the 1c page needs from 2e).

## 1. The goal
Phase 1 of contact lookup: a customer buys skip-trace (contact) lookups for the leads on a
results tab. The provider is Tracerfy, and the operator pays per credit (normal 1,
advanced 2). The chain, ALL LIVE except 2e:
1. **quote** `POST /jobs/{id}/contact-lookups/quote`: ONE Redis key per tab (TTL 600 s);
2. **confirm** `POST /jobs/{id}/contact-lookups` → 202 (**2d, LIVE 2026-10-02**): commits
   a `dispatching` action plus one `quoted` row per lead, then publishes the worker;
3. **worker** `lookup_contacts(action_id)` (2b): claims through the ONE claim path;
4. **reconciler** (2c): republishes, expires at 30 min, settles;
5. **billing** (O-C): per pending row; unmatched rows bill only if `rows_sent > 0 AND
   rows_uploaded >= rows_sent`.

**Contact lookups are PURCHASABLE in production now.** What is missing is a way for the
customer to SEE what they bought: **2e = `GET /jobs/{job_id}/contact-lookups/{action_id}`**
(status, counts, and the pause state), which the **1c frontend** page reads.

## 2. Where things are
| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup`. **NEVER the OneDrive checkout: its `.env` is PRODUCTION.** |
| Branch to continue on | **`feat/lookup-1b2e-status`**, off main `40a310f5`. It holds ONLY this handoff so far (pushed). |
| main | `40a310f5` (#431). 2d is `bd17ef2d` (#432). |
| Test env | `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY` = `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`), then `export DEBUG=false BL_TEST_REDIS_SERVER=C:/Users/Windows/bl-testenv/redis/redis-server.exe`. Local PG16 `bridgeleads_lookup1b_test` at rev **110**; Redis db 13. Bare `python` is dead Anaconda; that venv has no pip (uv). |
| Last session's scratchpad | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/c0ba9e9b-bb08-4194-9ef6-046e79b311e1/scratchpad/`: <br>`mut_2d.py` (55-mutant runner: anchors asserted, baseline guard, restore by content; copy the PATTERN for a `mut_2e.py`); <br>`mut_one.py` (one mutant vs one test node, hard timeout); <br>`2d_files.txt` + `2d_chunk_00..08` (the 60-file regression list, 9 chunks); <br>`codex_2d_review_r*.txt` / `codex_2d_rebase_check*.txt` + `_out.txt` (prompt templates). |
| Prod checks (read-only, from the OneDrive dir) | `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py`: the merge gate, read all 4 counts. **The auto-mode classifier BLOCKED me running it until the owner asked me to in their own message**; otherwise ask them to run it with `!`. Also `lock_holders.py`, `oc_*`, `app_pending_priv.py`. |
| Deploy check | `railway deployment list --service {api,worker,beat} --json` (`[0].status`, `[0].meta.commitHash`); `curl https://api.bridgeleads.io/health`; `railway logs --service worker`. A live-route probe: an unauthenticated POST/GET must be 401, not 404. |

## 3. State
**LIVE (merged under the standing rule, deploy verified):**

| PR | Merge | What |
|---|---|---|
| #421 / #422 / #424 | 10-01 | O-C: migration 110, `rows_sent`, the persisted billing decision |
| #425 + prod grant | 10-01 | O-D: worker DELETE on `pending_skip_trace_rows` (+ `skip_trace_cache` drift fixed) |
| #426 | `32658a17` | docs: the journal entry + the O-D records |
| **#432** | **`bd17ef2d`** | **2d: the confirm endpoint. Lookups purchasable.** api/worker/beat SUCCESS, unauth POST = 401, worker clean. |

Other sessions merged in between (their work, not mine): #427 docs, #429 pypdf 6.19.0,
#428 a DB-outage → 503 middleware (`src/api/middleware/database_unavailable.py`), #433 a
DB latency canary (beat task every 2 min, `canary_engine`), #431 docs. #430 (-f3, billing
copy) was next in the merge queue when I stopped.

**OPEN / not done:**
- **2e: NOT STARTED.** No spec, no consult, no code. The branch has only this file.
- **The 2d BUILD_JOURNAL entry: NOT written.** The owner said yes to journaling earlier;
  **ASK them before writing it** (and where: a docs PR or riding with 2e). Material is in
  §5 and §6.
- The plan file on main has no "### 2d MERGED + LIVE" line yet: add it on this branch.

## 4. Active files
**2d (LIVE; 2e reads what it wrote)** in `src/api/routes/jobs.py`:
- the quote section from `:1092`; `quote_contact_lookups` `:1161`; it reads the pause
  state via `read_pause_state` at `:1254` (`src/utils/skip_trace_pause_state.py:225`;
  **2e should reuse exactly this reader and its `_bounded` call**);
- `_canonical_job_id` `:1131`: **2e MUST use it** (shared by quote and confirm; malformed →
  404, the canonical form feeds every cast and comparison);
- the confirm section from `:1326`: `_snapshot_json` `:1379`, `_valid_quote_payload` `:1394`,
  `_action_for_quote` `:1432`, `_replayed` `:1443`, `_publish_contact_lookup` `:1462`, the
  route `confirm_contact_lookups` `:1503`.
- `src/api/schemas.py:2000-2120`: `ContactLookupQuote*`, `ContactLookupPause` `:2019`
  (reuse for 2e's pause field), `ContactLookupUnavailable*`, `ContactLookupConfirmRequest`
  `:2085`, `ContactLookupAction` `:2095`, `ContactLookupConfirmError*`.
- `tests/test_contact_lookup_confirm.py` (73 tests): helpers `_confirm`, `_quoted`,
  `_action`, `_events`, `_set_quote`, `_stamp_refused`, the `published` pass-through spy;
  it imports `_auth, _job, _quote, _seed, _stored` from `tests/test_contact_lookup_quote.py`.
  2e's tests should build REAL actions through the real quote + confirm, then run the
  real worker (`cla.run_action(aid)`) and the real reconciler
  (`rec._reconcile_contact_lookups_impl()`) to reach each state.
- The data 2e reads: `alembic/versions/101_contact_lookup_action_schema.py` (tables
  `contact_lookup_actions`, `contact_lookup_action_results`, `contact_lookup_action_events`;
  the guard triggers; RLS) + 107 (`quote_snapshot`, `dispatched_at`) + 110.
  `src/workers/contact_lookup_action.py` (`run_action` `:488`, `lookup_contacts` `:531`)
  and `src/workers/scheduler_helpers/contact_lookups.py` (P1-P3) are the writers of every
  status and count.

## 5. Changes made in the 2d session (beyond the PR table)
- 2d review history (all in the plan): Codex r1-r5 NO-GO, r6/r7 GO, then THREE
  post-rebase checks because main moved four times. Every r2-r5 finding was one class:
  **a corrupt stored quote 500s after the gates**:
  - non-object JSON;
  - `remaining` / `stopped` invalid (`_QUOTE_STOPS` is pinned to the planner by an AST
    source scan);
  - a price overflowing INTEGER;
  - `NaN`, and **`1e9999`** (a JSON NUMBER that parses to inf);
  - NUL (jsonb + text columns), lone surrogates, nesting depth (16, iterative check);
  - a malformed / upper-case `job_id`.
- **The post-#428 check found a REAL interaction:** a failed rollback after a failed
  post-commit stamp escaped, and #428's middleware made it a 503 for a COMMITTED purchase.
  Fixed (`ba306b8b`, a guarded rollback + a labelled double-fault test).
- The quote route now shares `_canonical_job_id` (a LIVE behaviour change: malformed → 404).
- Mutation: 55 mutants, 54 caught + 1 judged equivalent by Codex (AO5's constraint filter).
  Regression 60 files: 1,457 passed, 0 failed.
- Memory updated: `project_lookup_1b1a_2026_09_25.md` (the 2026-10-02 section) + its index
  line.

## 6. Failed attempts / traps (don't repeat)
- **Host memory pressure KILLS background jobs** (twice this session). The first kill hit
  mid-mutant and **left a live mutant in `jobs.py`** (`git diff --stat` looked like a
  2-line change). Caught by `sha256sum -c`, restored by `git checkout`.
  - After ANY runner stop, hash-check.
  - Prefer foreground slices of ≤3 mutants (~100 s baseline + ~50-100 s each). A
    foreground call over 600 s auto-moves to the background; wait on its output file with
    an `until grep` loop.
- **I edited `jobs.py` while a regression was running against it, twice.** The run was
  void and had to be stopped and redone. **Get Codex GO FIRST, then start the regression.**
- **My reasoning was refuted by Codex (r4):** "refusing NaN / Infinity constants means no
  non-finite float can reach the snapshot" is WRONG, because `1e9999` parses to inf.
  Validate at the SINK (`json.dumps(allow_nan=False)` + encode + NUL + depth), not at the
  parse.
- **A 200,000-char parametrize value became the test node ID** → 2 opaque ERRORs with no
  traceback. Give big params `pytest.param(..., id="...")`.
- **`git show HEAD:schema/openapi.json | grep -c $'\r'` reported 9,101 CRs: a false
  alarm.** `git show` converts on output; check `git cat-file blob HEAD:path` instead. The
  repo blob is LF (`eol=lf`); the working copy is CRLF.
- A full-file mutant run "timed out" (INCONCLUSIVE) on a mutant that 500s: an escaped
  exception can hang a later teardown. Re-run that mutant against its target test alone
  (`mut_one.py`); it was caught in 15 s.
- **Rebasing onto another session's GLOBAL change (a middleware) is a semantic change**,
  even when the diff is byte-identical. Ask Codex what it does to your error paths.
- **A merge-slot claim must go to EVERY peer** (ListAgents). I told only -f3, and -db merged
  #433 in between, forcing another rebase + CI.
- **Branch protection requires up-to-date branches:** every main move = rebase + byte proof
  + Codex re-check + ~24 min CI.

## 7. NEXT STEP (where I stopped) — on branch `feat/lookup-1b2e-status`
1. **Ask the owner two things up front:**
   - write the 2d BUILD_JOURNAL entry, and where (a docs PR, or riding with 2e);
   - confirm 2e as the next work.
2. **Spec 2e in the plan** (under "### 1b-2e"; replace the stub), read from the code:
   - **Route:** `GET /jobs/{job_id}/contact-lookups/{action_id}`, `get_rls_db`,
     `_canonical_job_id` for `job_id`; `action_id` parsed as a UUID too (malformed → 404).
   - **Ownership:** the action must belong to this user AND this job (404 otherwise; RLS
     is the belt, the `user_id` filter the suspenders).
   - **Body:**
     - `status` + `status_reason`, the timestamps (`created_at`, `dispatched_at`,
       `started_at`, `claimed_at`, `settled_at`);
     - the counts (`quoted_count`, `claimed_count`, `reused_count`, `newly_queued_count`,
       `billable_rows`, `tracerfy_credits`) and `truncated`;
     - the pause state (the quote's `read_pause_state` path, same bound and UNKNOWN
       fallback);
     - never lead ids or PII.
   - **Rate limit:** decide the zone with Codex (a 1c page polls this).
   - **Questions for Codex:**
     - are the counts a cache that can be stale vs `contact_lookup_action_results`
       (101 says "A CACHE ... fully recomputable")? Derive or read?
     - what the 1c page needs (§ "Phase 1c");
     - the polling cost;
     - the per-status semantics (`expired`, `failed`, settled).
3. **Codex pre-code consult, FOREGROUND, until `PLAN: GO`.**
4. Build the files (5-file rule): route in `jobs.py`, schema in `schemas.py`, `openapi.json`
   (`$PY scripts/export_openapi.py`, then `--check`, 0 deletions vs main), a NEW
   `tests/test_contact_lookup_status.py`, the plan.
5. A mutation runner `mut_2e.py`, then a Codex three-dot diff review to `GATE: GO`, THEN
   the regression (chunk lists in the scratchpad; add the new test file), then the PR → CI
   → quiet (owner) → merge → deploy verify (401 probe).
6. Then the 1c frontend (the sibling repo; see memory `frontend_ui_state_conventions`).
7. Small follow-ups (separate PRs):
   - every other `/jobs/{job_id}` route still turns a malformed id into a 500 (with a ref,
     no stack): move them onto `_canonical_job_id`;
   - `scripts/deactivate_test_batch_configs.py:3-5` and
     `scripts/purge_test_batch_configs.py:54-59` still say the system role has
     "DELETE=False on every table".

## 8. Rules (binding, from the owner)
- **Codex in the loop on every step, FOREGROUND:**
  - a pre-code consult to `PLAN: GO`;
  - a THREE-DOT diff review to `GATE: GO`;
  - after every rebase, a byte-identical diff proof (`git diff OLDBASE...OLDHEAD |
    grep -v '^index '` vs `git diff origin/main...HEAD | grep -v '^index '`, `cmp`) plus a
    Codex re-check.
  - Invocation: `codex exec "$(cat prompt)" -s read-only -c 'model_reasoning_effort="high"'
    -c 'mcp_servers={}' --skip-git-repo-check < /dev/null > out 2>&1`. Open every prompt with
    "Do NOT load any skill, do NOT run /graphify or any preamble. Read-only: ..." and tell
    it NOT to run pytest. Read the verdict after the LAST `tokens used` line.
- **Tests:** real PG + Redis, no mocks. A pass-through spy is OK; fault injection only
  where unavoidable, labelled, and it must PROVE it ran (a counter or a log line).
- **A mutation runner per PR:**
  - `df -h /c` first (12 GB free last time); COMMIT before mutating;
  - assert every anchor; refuse a red baseline;
  - restore verified by hash; read WHICH test failed; a timeout is INCONCLUSIVE.
- **Regression** in 7-8-file chunks, output to files. ruff clean; no type checker is
  configured (say so).
- **The 5-file rule** (the plan counts; a handoff rides outside). **A merge is a deploy.**
- **Standing merge rule:** `quiet.py` all 4 counts 0 (re-run if one is transiently
  non-zero), CI green on the EXACT head, Codex GO, main unchanged,
  `gh pr merge N --merge --match-head-commit <sha>`. Never `--admin`. Send "merging" /
  "verified" to EVERY peer from `ListAgents`.
- Before killing any process, confirm it is YOURS (`Get-CimInstance Win32_Process` filtered
  on `pytest`). Files are CRLF in the working copy: check `grep -c $'\r$'` == `wc -l`
  after edits.
- Ask the owner before any production write and before writing a BUILD_JOURNAL entry.
- Another session (-db) asked: **no full-table scans of `results` on prod**.
