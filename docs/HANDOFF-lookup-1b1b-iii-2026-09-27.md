# HANDOFF — contact lookup, Phase 1b-1b-iii (the spend cap's PAUSE STATE), 2026-09-27

Read this whole file before touching anything. Then read, in order:
1. `tasks/todo-lookup-contacts.md`, these parts (search the quoted text):
   - **"D3 Daily cap. ANSWERED"** (near the top): the user-facing contract for the pause.
   - **"15-10 daily-cap resume time"** and **"15-11 (VERIFIED) the global cap is SOFT"**.
   - **"C8-C10 (P2) Redis contract"** and **"C13 (P2) scope"** (in the 1b-1b pre-code consult).
   - **"Revised split (ACCEPTED by owner, 5-file rule)"**: the one-line 1b-1b-iii scope.
2. `src/workers/skip_trace_dispatcher.py`: the top of `dispatch_pending_skip_trace` (the
   early global-cap check, `return {"skipped": "daily_cap", ...}`, ~line 100) and the in-lock
   cap block (search `resolve_caps()` inside the `for trace_type` loop, and `global_rows == 0`).
3. `src/workers/skip_trace_capacity.py` (the caps, `spent_credits`, `row_allowance`).
4. `src/workers/scheduler.py` around the `"dispatch-pending-skip-trace"` beat entry
   (`"schedule": 300.0`, hardcoded today).

## Where things are

| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup` (NOT the OneDrive checkout; that one's `.env` is PRODUCTION) |
| Branch | **`feat/lookup-1b1b-iii-pause-state`**, cut from `origin/main` `f80f79ce`, holds only this handoff commit. Local only, never pushed. **Nothing of 1b-1b-iii is built yet.** |
| Test DB | local PG16 `bridgeleads_lookup1b_test`, now at revision **104**. Env: `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY`; set `export DEBUG=false` or SQLAlchemy echoes every statement). If PG/Redis are down, see memory `reference_local_full_pytest_2026_07_03`. |
| Prod checks | read-only scripts in `C:/Users/Windows/bl-checks/` (`quiet.py`, `p102.py`, `p103.py`, `pgdef.py`, `cap.py`). Run from the OneDrive repo dir: `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py`. Claude can run them itself (worked all day 09-27). |
| Last session's scripts | scratchpad of session `22432d26`: `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/22432d26-216c-4f01-a58e-1b6bedd4bbaa/scratchpad/`: `gate_iic2.py` (the refill perf gate: cases A/B/C, `GATE_INTERLEAVE`, `GATE_EXPLAIN=0,5,11`, `GATE_FILLER`, `GATE_KEEP`, `GATE_DUMP_SQL`), `plan_variants.py`, `mutate.py` / `mutate_safety.py` / `mutate_compose.py` (CRLF-aware mutation runners: the pattern to copy), `codex_*.txt` prompts. |

## The goal

Phase 1b-1b = a hard, fair, credit-weighted daily spend cap on the skip-trace dispatcher (it
SPENDS the operator's money at Tracerfy; normal lookup = 1 credit, advanced = 2; two rolling-24h
caps, global and per account). **1b-1b-iii makes the pause VISIBLE**: when a cap binds, the
dispatcher computes WHEN dispatch can resume and publishes it to Redis with a heartbeat, so the
API (quote dialog, results page: Phase 1c) can say "lookups are paused until X" instead of an
endless "looking". The API never reads the queue tables (its role has no grant: memory
`landmine_worker_only_tables_unreadable_by_api_role`); Redis is its only source.

## State: everything below is LIVE in production

| Step | PR | Merge |
|---|---|---|
| 1b-1b-i spend-ledger hardening | #358 | live |
| ii-0 retire out-of-dispatcher spenders | #359 | live |
| ii-a migration 102 (weight CHECK, guard trigger, spent index) | #361 | live |
| ii-b the cap (in-lock READ COMMITTED spend read, fair selection, refill) | #364 | live; prod caps `SKIP_TRACE_DAILY_CREDIT_CAP=2000`, `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP=500` |
| ii-c-1 migration 103 (keyset frontier index) | #365 | live, verified by the object |
| **ii-c-2 keyset refill** | **#366 `2e839076`** | **live 09-27 10:00Z**; first tick OK but on an EMPTY queue: the walk has not yet run on real rows |
| **Alembic safety fix** | **#370 `2b397c82`** | live 09-27 12:32Z |
| **docs (journal + plan)** | **#372 `c9954f9f`** | live |
| **local docker-compose never reads `.env`** | **#373 `f80f79ce`** | live 09-27 15:00Z |

## What the last session did (changes, and what failed)

**#366 ii-c-2.** Refill walks a per-account keyset frontier over migration 103 instead of
re-ranking every queued row each round.
- **The round-0 blocker's real cause:** the watermark `enqueued_at <= const` inside the lateral
  paired with the `enqueued_at >= after_at` the planner derives from the frontier compare into a
  range priced at a flat 0.5%, so the planner walked the OLD `ix_pending_skip_trace_dispatch`
  and filtered out 64,680 other-account rows per account (1,172 ms).
- **The fix:** the watermark is applied in `allocate()` after the walk (1.2 ms), with
  `enable_bitmapscan=off` inside a savepoint.
- Also `_in_flight_keys` now reads only the 7 key columns (1,331 → ~300 ms).
- **Failed first:** I added `user_id` to the lateral's ORDER BY. Useless: the planner already
  treats it as fixed.
- The old gate seed stored each account's rows contiguously in time, which HID the bad plan.
  Always seed interleaved too.

**#370 safety.** `alembic/env.py` called `load_dotenv()`, which searches from `alembic/`, so a
checkout whose `.env` is production gave Alembic the prod owner role. Now:
- `src/db_safety.py` is the shared test-DB classifier (also refuses a `service` query key,
  requires an explicit port, refuses `PGHOSTADDR`/`PGSERVICE`/`PGSERVICEFILE`/`PGSYSCONFDIR`).
- The pytest guard PINS `DATABASE_URL_MIGRATE`.
- env.py never reads `.env`, and under `ENVIRONMENT=test` refuses any non-test target.
- 42 tests, 10 mutations.

**#373.** docker-compose app services read `.env.local` only:
- no `${}` interpolation;
- DB and Redis pinned, port-less (the sync URL `:5432/`→`:6543/` rewrite);
- paid/destructive switches pinned off;
- no committed `SECRET_KEY`.

**Failed / cost time (don't repeat):**
- A bash heredoc mangled `\n` in a Python edit → use the Edit tool or Write tool for scripts.
- A gate run's seeding hit the statement timeout and leaked 50 users into the test DB (emails
  are field-encrypted, so `gate_%` lookups miss them). Clean up by creation time; gates now
  clean up at exit.
- `git diff origin/main..HEAD` after `main` moved made Codex report other PRs as reversions
  (twice). **Fetch + rebase before every Codex review.**
- `main` moved under every PR (#367, #368, #369, #371). Branch protection REQUIRES an up-to-date
  branch, so each move = rebase + ~17 min CI. Never `--admin`.
- The rebase refused while `tasks/todo-lookup-contacts.md` was dirty. Do NOT `git stash` (shared
  across worktrees): copy the file aside, `git checkout --` it, rebase, copy back.
- One merge call had a malformed SHA (rejected, harmless). Always pass `git rev-parse HEAD`.

## Next step: 1b-1b-iii, from the plan (nothing built yet)

**Starting point in code:**
- The early global-cap check returns `{"skipped": "daily_cap"}` and sends an ops alert.
- The in-lock path `continue`s when `global_rows == 0` and only logs.
- NOTHING computes a resume time, writes Redis, or reads it.
- The beat interval is hardcoded `300.0` in `scheduler.py`.

**Requirements already decided (binding; see the plan sections listed at the top):**
- **Resume time (15-10):** the `(spent - cap + 1)`-th oldest `submitted_at` in the window
  (cumulative CREDITS, since normal=1 and advanced=2), + 24h, ordered deterministically by
  `(submitted_at, tracerfy_queue_id, id)`, with a small margin for the `>=` boundary. Computed
  by the DISPATCHER (it has the grant).
- **Redis contract (C8-C10, D3):**
  - A namespaced key.
  - Publish EVERY account at or over its cap, whether or not it has queued rows (the quote
    comes before any row exists), plus the global state.
  - A `published_at` heartbeat, so a stale or missing publish reads as UNKNOWN, never "not
    paused".
  - The API reads only `HGET <own user_id>`, never the whole hash.
  - Values are compared with now.
  - Refreshed on every paused tick; deleted on the first tick that resumes.
  - Redis unreachable → the field is absent and nothing else breaks.
  - ADVISORY only: it never gates spending (the in-lock DB read does).
- **TTL:** derived from the EFFECTIVE beat interval, `max(2 * interval + grace, resume_at - now
  + grace)`, never hardcoded 600s (beat intervals reset on every deploy: memory
  `landmine_beat_intervals_reset_on_every_deploy`).
- **C13:** the beat interval becomes a setting that `scheduler.py` consumes (settings +
  `.env.example`; NOTE Claude's reads of `.env.example` are denied by a rule, so ask the owner or
  add the line blind with the Edit tool only if allowed).
- **Tests:** both directions (stays while paused, no stale "paused" after resume), heartbeat
  staleness, a per-account cap vs the global cap, Redis down.

**Process (owner's working agreements, binding):**
1. Write the plan into `tasks/todo-lookup-contacts.md` (a new "Phase 1b-1b-iii" section with
   checkboxes, and the 5-file split).
2. Codex pre-code consult until `PLAN: GO`, then CHECK THE PLAN WITH THE OWNER before code.
3. Build with tests, mutation proofs, and the regression batches (lists: see the 1b-1b-ii-c
   section's "Regression" bullet).
4. Codex diff review until `VERDICT: GO`.
5. Fetch + rebase, push, PR, CI (foreground, two ~10 min windows).
6. **Merge under the owner's STANDING rule** (memory `feedback_standing_merge_ok_lookup_queue`):
   Claude may merge its own PR in this queue when `quiet.py` is all zeros, CI is green on the
   exact head, Codex says GO, and `main` hasn't moved; merge with `--match-head-commit <full
   sha>`, then verify the Railway worker deploy by commit and the boot logs.
7. Append a `docs/BUILD_JOURNAL.md` entry (newest on top).

After 1b-1b-iii: Phase 1b-1c / 1b-2 per the plan. **H3 carries into 1b-2:** the action worker
uses `lock_job_for_claim()` + `claim_skip_trace_rows()` only, never a direct queue insert or an
in-flight row, with a two-session test while `_CLAIM_LOCK_KEY` is held.

## Working agreements and gotchas

- **Codex in the loop on EVERY step.** Run it in the FOREGROUND:
  `timeout 590 codex exec "$(cat prompt.txt)" -c 'model_reasoning_effort="high"' -c
  'mcp_servers={}' --skip-git-repo-check < /dev/null > out.txt`. Start every prompt with "Do NOT
  load any skill, do NOT run /graphify or any preamble". Say "read-only: do NOT edit files, run
  pytest/alembic, or touch any database". Background runs get reaped for low memory.
- **Codex wins where docs are silent**, unless its fix breaks money safety. A reasoned pushback
  that cites its OWN earlier acceptance has worked twice. Record every reconciliation in the plan.
- **5-file rule per PR** (the plan file counts). Split into phases instead of exceeding it.
- **Files are CRLF:** Python `str.replace` with LF anchors silently no-ops. Use the Edit tool,
  or byte-level with `\r\n`.
- **Merge IS deploy** (Railway migrates on boot via `scripts/migrate.py`).
- **No mocks** except the Tracerfy boundary: tests use the `http://` base-URL trick, a definite
  rejection that releases every claimed row to `errored`.
- **Logged follow-ups (not in 1b-1b-iii):**
  - `main.py` always allows the production CORS origins;
  - CI could set the libpq variables empty;
  - `bootstrap.sh` and `docs/product/backend-build.md` wording;
  - the dormant `docker-compose.prod.yml` (audit S3-53);
  - `.env.example` comments (owner).
