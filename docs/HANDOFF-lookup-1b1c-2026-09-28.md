# HANDOFF — contact lookup, after 1b-1b-iii (the spend cap is DONE) -> Phase 1b-1c, 2026-09-28

> **STATUS UPDATE (2026-09-28, later the same day):** the 1b-1c plan reached Codex `PLAN: GO`
> and the owner approved it; **1b-1c-i (planner + pricing) is BUILT** and ships in the PR that
> carries this file. **Next: 1b-1c-ii, the quote endpoint.** The plan file's "FINAL 1b-1c
> contract and build list" and "1b-1c-i BUILT" sections supersede "Next step" below; the two
> "Open items" are closed (the prod pause hash was read live; the journal entry is in this PR).

Read this whole file before touching anything. Then read, in order:
1. `tasks/todo-lookup-contacts.md`, these parts (search the quoted text):
   - **"### Revised split and ORDER"** (~line 827): 1b-1a -> 1b-1b -> **1b-1c PLANNER + QUOTE**
     -> 1b-2 WRITERS. 1b-1a and all of 1b-1b are live. **1b-1c is next.**
   - **"D3 Daily cap. ANSWERED"** (~line 62) and its 2026-09-27 note: the quote shows
     `paused_by_daily_cap` + a resume time. The Redis contract it must read is the v1 hash below.
   - **"`plan_contact_lookup(results) ->`"** and **"`POST /jobs/{job_id}/contact-lookups/quote`"**
     (~lines 556-580): the 1b-1c spec as written in 2026-09-20. It predates rounds 16-21 and the
     whole 1b-1b work: re-read it against the round-16 "Revised split" and the carried items.
   - **"### FINAL contract and build list"** (Phase 1b-1b-iii) + the r4/r5 amendments: the pause
     state the quote must read (only through `read_pause_state()`).
   - **"Carried into 1b-2, do not lose"** (~line 2789) and **"H3 carried to 1b-2, exactly"**.
2. `src/utils/skip_trace_pause_state.py`: `read_pause_state(r, user_id, now) -> PauseState`, the
   ONLY way the API may read the pause state.
3. `docs/HANDOFF-lookup-1b1b-iii-2026-09-27.md`: the previous handoff (process, gotchas).

## Where things are

| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup` (NOT the OneDrive checkout; its `.env` is PRODUCTION) |
| Branch | **`feat/lookup-1b1c-planner-quote`**, cut from `origin/main` **`c0b09b7a`**, holding only this handoff commit. Local only, never pushed. **Nothing of 1b-1c is built.** |
| Test DB | local PG16 `bridgeleads_lookup1b_test`, now at revision **105**. Env: `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY`; set `export DEBUG=false` or SQLAlchemy echoes every statement). Local Redis is used by the tests (conftest flushes it, localhost only). |
| Prod checks (read-only) | `C:/Users/Windows/bl-checks/`: `quiet.py` (merge gate), `p105.py` (105 by the migration's own `_is_right_shape()`; needs `MIG105=<path to the 105 file>`), `show_settings.py` (prod planner settings), `alembic_where.py`, `pause_state.py` (see "Open items": it CANNOT reach prod Redis from here). Run from the OneDrive dir: `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py`. |
| This session's scripts | scratchpad of session `4fe51d38`: `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/4fe51d38-c564-4bfb-b9b5-c5ee8718a53d/scratchpad/`: `gate_iiib.py` (100k-row seed, VACUUM ANALYZE, atexit cleanup: the seed the others exec), `gate_iiib_k5.py` (the owner-approved relative gate: `uniform`/`skewed`, prod planner settings, concurrent writer, plan assertions), `gate_iiib_v2.py` (index prototype), `gate_spent_baseline.py`, `mutate_iiib.py` / `mutate_105.py` (CRLF-aware, exact-once anchors, restore byte-for-byte), `codex_*.txt` prompts + `_out.txt` outputs. |

## The goal

Phase 1 of contact lookup: a customer can buy skip-trace lookups (Tracerfy, the operator pays
per credit: normal 1, advanced 2) for leads on a results tab. **1b-1b** made the spend cap hard,
fair, credit-weighted, and VISIBLE. **1b-1c** is the read path the customer sees first: a pure
planner (`plan_contact_lookup`) shared by quote and worker, the immutable quoted set, and
`POST /jobs/{job_id}/contact-lookups/quote`, which must also report the pause state.
**1b-2** then adds the writers (confirm, worker claim, ledger, settlement). 1c is the frontend.

## State: everything below is LIVE in production

| Step | PR | Merge |
|---|---|---|
| 1b-1b-i ledger, ii-0, ii-a (mig 102), ii-b cap, ii-c-1 (mig 103), ii-c-2 keyset refill | #358-#366 | live (earlier sessions) |
| Alembic safety, docs, local compose never reads `.env` | #370, #372, #373 | live |
| **iii-a** `SKIP_TRACE_DISPATCH_INTERVAL_SECONDS` (default 300, 60..599), beat reads it | **#376 `bca09eff`** | live 09-28; beat sent the dispatch task exactly boot+300 s |
| **mig 105** `ix_pending_skip_trace_account_spent (user_id, submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL` | **#379 `29afc82e`** | live 09-28; verified in prod by its own `_is_right_shape()` (covering 1,002 spent rows) |
| **iii-b** the pause state published to Redis every tick | **#382 `c0b09b7a`** | live 09-28 ~10:32Z; first tick 10:36:38Z succeeded, 0 "pause state not published" warnings |

Prod caps: `SKIP_TRACE_DAILY_CREDIT_CAP=2000`, `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP=500`. Prod DB
is PostgreSQL **17.6** with `work_mem 2184kB`, `random_page_cost 1.1`,
`effective_cache_size 384MB`, `jit off` (local is PG 16.14: SET LOCAL these in any perf gate).

## What is built (the contract 1b-1c consumes)

- **Redis hash `bridgeleads:skip_trace:pause:v1`** (`src/utils/skip_trace_pause_state.py`,
  stdlib-only so the API can import it without building the Celery app): fields `fence`
  (20-digit zero-padded Postgres xid), `published_at`, `fresh_until` (ISO UTC), `global`,
  `account_default`, one `<user_id>` per paused account; each scope is exactly
  `{"normal_resume_at": iso|null, "advanced_resume_at": iso|"never"|null}`. Written every tick
  by one Lua script that replaces the hash only for a NEWER fence; a switched-off dispatcher
  writes a fenced tombstone (`state=disabled`, reads UNKNOWN).
- **`read_pause_state(r, user_id, now)`** returns `PauseState(status, normal_resume_at,
  advanced_resume_at)` with status `paused` / `not_paused` / `unknown`. HMGET of a FIXED field
  list only. Missing/stale/malformed/undecodable/Redis-down -> UNKNOWN, never "not paused".
  Per cost: `"never"` dominates, else the later of account and global; a past time = not paused.
  **The quote must show UNKNOWN honestly** (e.g. "we could not check the daily limit right now"),
  never as "not paused". `"never"` = "advanced lookups are unavailable under the current limit".
- `resume_times()` (`src/workers/skip_trace_capacity.py`): ONE statement, early-stop walks over
  105 (accounts) and 102 (global). `_publish_pause_state()` / `_pause_fence()` /
  `_dispatch_tick()` in `src/workers/skip_trace_dispatcher.py` (publisher runs in `finally`,
  own session, READ COMMITTED, one fence SELECT, `SET LOCAL statement_timeout = '5s'`, session
  closed before Redis I/O).

## What this session did, what failed, what was learned

- **iii-a** straightforward (Codex GO first review). 904 regression passed.
- **iii-b first build FAILED its gate** (window-function resume query sorted the whole 24h window:
  193-404 ms at 100k rows vs the approved 100 ms). Query-only variants could not fix it (~200 ms).
  Codex: RECOMMEND A = the plan's own route, an account-leading index first (**105**).
  Prototyped on the test DB before building: with 105 the walks cost ~12 ms; the rest is ONE pass
  of per-account totals, the same work the live `spent_credits()` pays. So no design meets a flat
  100 ms at 100k rows. **Owner chose the RELATIVE gate (K5):** reachable p95 <= live
  `spent_credits()` p95 + 60 ms; lowered-cap max <= 1 s; plan shows 105/102, no spill.
  Rebuilt query PASSED on uniform and skewed seeds (reachable 36-100 ms vs budgets 95-141 ms;
  lowered cap <= 368 ms).
- **Gate pitfalls hit (don't repeat):** a fresh seed without VACUUM has no visibility map, so
  index-only scans heap-fetch everything (baseline read 347 ms instead of 60 ms): VACUUM ANALYZE
  the seed. An EXPLAIN that reuses caps read BEFORE a concurrent-writer case puts the scope OVER
  its cap (walks 239-893 rows): re-read caps at EXPLAIN time. The totals' full pass over 105 is
  NOT a "walk" (walks are the looped lateral scans).
- **105 test pitfalls:** 103's INVALID-index test used a corpse that was also the wrong shape, so
  `indisvalid` was never isolated (a mutation survived). An idle READ COMMITTED reader holds no
  snapshot (the build never waits); a ROW EXCLUSIVE holder stops the build BEFORE `indisready`.
  What works: a REPEATABLE READ snapshot holder, the build on its own thread, poll
  `pg_stat_progress_create_index` for `waiting for old snapshots` with the index ready+invalid,
  then `pg_cancel_backend`. Codex also required `statement_timeout` on CONCURRENTLY builds
  (`lock_timeout` does not end the wait for old transactions).
- **A wrong assumption of mine:** I split the fence SELECT believing `pg_current_xact_id()` raises
  in a read-only transaction. TESTED: it does not. Codex's one-SELECT form was adopted.
- Codex review NO-GOs fixed: `\d` accepts non-ASCII digits (now `[0-9]{20}`); undecodable Redis
  bytes raised out of the reader (now UNKNOWN). Every fix has a mutation proof.
- A background regression run was reaped for low memory: run big batches in the FOREGROUND in
  ~3 chunks of ~8 files (`timeout 590`). Final iii-b regression: 824 passed, 0 failed.
- Rebasing a branch whose plan file conflicts with main: take main's version when main already
  contains the edits (`git checkout --ours`), skip the then-empty commit. Never `git stash`.

## Open items (owner decisions pending, asked at the end of the session)

1. **Positive prod check of the published hash.** Prod Redis is private
   (`redis.railway.internal`); `railway run` runs LOCALLY and cannot resolve it. The only way is
   `railway ssh --service worker -- <cmd>` (a read-only HMGET inside the container). Not
   authorized yet by the standing rule (it covers commit + boot-log checks). **Ask first.**
   Current evidence: the tick succeeded and 0 "pause state not published" warnings.
2. **`docs/BUILD_JOURNAL.md` entry for this session** (built / failed / learned above). Docs-only
   PR; a merge IS a deploy. **Ask first**, or fold it into the first 1b-1c PR if the owner prefers.
3. The plan's **"### Still open"** section (~line 2818) is STALE ("Phase 1b-1 has NOT started"):
   update it when 1b-1c's plan is written.

**Logged follow-ups (not in 1b-1c):** migrations 102/103 lack the `statement_timeout` 105 has;
103's INVALID test never isolates `indisvalid`; `tests/conftest.py`'s Redis fixture ignores
`redis_kwargs()` and never closes its client; `main.py` always allows the prod CORS origins; CI
could set the libpq variables empty; `docker-compose.prod.yml` (audit S3-53).

## Next step: Phase 1b-1c PLANNER + QUOTE (nothing built)

1. Re-read the 1b-1c spec (lines ~556-600) against everything since: the round-16 revised
   order, rounds 19-21 (the 1b-1a schema that is live: `contact_lookup_actions`,
   `_action_results`, `_action_events`, `results.last_trace_outcome`, `action_id`), H3, the
   D3/C8-C10 notes (the quote reads the pause state ONLY via `read_pause_state()`; the API role
   has NO grant on the queue tables: memory `landmine_worker_only_tables_unreadable_by_api_role`).
2. Write the 1b-1c plan into `tasks/todo-lookup-contacts.md` (checkboxes, a 5-file split: the
   planner + quote endpoint + schemas + tests will likely need two PRs; the OpenAPI regen rule
   is memory `reference_openapi_regen_env_matters`, and FE types are generated from it).
3. Codex pre-code consult until `PLAN: GO`, then **CHECK THE PLAN WITH THE OWNER** before code.
4. Build with tests + mutation proofs + regression batches; Codex diff review to `VERDICT: GO`.
5. Fetch + rebase (three-dot diff for every Codex review), push, PR, CI (foreground, two ~10 min
   windows), merge under the standing rule, verify the deploy by commit and logs.

## Working agreements and gotchas (binding)

- **Codex in the loop on EVERY step, FOREGROUND:** `timeout 590 codex exec "$(cat prompt.txt)"
  -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check < /dev/null >
  out.txt`. Start every prompt with "Do NOT load any skill, do NOT run /graphify or any
  preamble" and "Read-only: do NOT edit files, run pytest/alembic, or touch any database or
  Redis". Always review `git diff origin/main...HEAD` (THREE-dot) after fetch + rebase.
- **Codex wins where docs are silent**, unless it breaks money safety; record every
  reconciliation in the plan. Test a disputed assumption before arguing it.
- **5-file rule per PR** (the plan file counts). **Merge IS deploy** (Railway migrates on boot
  via `scripts/migrate.py`; merge-time quiesce if a migration needs it).
- **Standing merge rule** (memory `feedback_standing_merge_ok_lookup_queue`): Claude may merge its
  own PR in this queue when `quiet.py` is all zeros, CI green on the exact head, Codex GO, and
  `main` unchanged: `gh pr merge N --merge --match-head-commit <full sha>`. Never `--admin`.
  Then verify api/worker/beat on the commit (`railway deployment list --service X --json`,
  `meta.commitHash`) and the boot/tick logs.
- **Files may be CRLF:** use the Edit tool, or byte-level Python with `\r\n`; bash `/tmp` is
  invisible to Windows Python (write scripts to the scratchpad).
- **No mocks** except the Tracerfy boundary (`http://` base URL -> definite rejection).
  Monkeypatched seams used in tests: `_dispatch_tick`, `resume_times`, `redis.from_url`,
  `system_sync_session` (wrappers around the real ones).
- `alembic_version` reads EMPTY to non-owner roles (RLS on, no policy): verify migrations BY THE
  OBJECT, never by `alembic_version`.
- Bare `python` is dead (anaconda removed): use `$PY` from the env script, or
  `C:/Users/Windows/bl-rescat-venv/Scripts/python` for the prod checks.
