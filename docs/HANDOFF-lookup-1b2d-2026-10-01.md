# HANDOFF: contact lookup Phase 1b-2d, the confirm endpoint (2026-10-01)

Read this whole file first. Then read these sections of `tasks/todo-lookup-contacts.md`
(search the headings). Together they are the NORMATIVE contract:
- **"## Phase 1b-2d — the confirm endpoint"**: the spec, Facts, Design steps 1-10, consult
  r1-r7 (AO1-AO5), PLAN: GO;
- **"### 2d BUILT"**: what is built and the NEXT list.

For background, if needed: "## Phase 1b-2 — the WRITE path" (V1, V6, V7, W4, W6) and
"## Phase 1b-2c — the reconciler" (P3 re-publish, AA1 deadline).

## 1. The goal
Phase 1 of contact lookup: a customer buys skip-trace (contact) lookups for the leads on a
results tab. The provider is Tracerfy, and the operator pays per credit (normal 1, advanced 2).
The chain:
1. **quote** (LIVE, 1b-1c): `POST /jobs/{id}/contact-lookups/quote` stores ONE Redis key per
   tab;
2. **confirm** (2d, THIS WORK): `POST /jobs/{id}/contact-lookups` commits a durable
   `dispatching` action plus one `quoted` row per lead, then publishes the worker;
3. **worker** (LIVE, 2b): `lookup_contacts(action_id)` claims the leads into the paid queue
   through the ONE claim path;
4. **reconciler** (LIVE, 2c): re-publishes, expires at 30 min, settles;
5. **billing** (LIVE, O-C): per pending row at ingest; unmatched rows bill only if
   `rows_sent > 0 AND rows_uploaded >= rows_sent`, and the decision is persisted.

**2d is the LIVE SWITCH.** Once deployed, a paying customer can buy lookups. Owner (2026-10-01):
"start 2d"; O-A: it goes live with no flag.

## 2. Where things are
| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup`. **NEVER the OneDrive checkout: its `.env` is PRODUCTION.** |
| Branch to continue on | **`feat/lookup-1b2d-confirm`** (pushed; head `a1fd3651` + this handoff). STACKED on `docs/lookup-od-merged` (= PR #426). No PR for 2d yet. |
| main | `9bfc73b3` (#425) at handoff. |
| Test env | `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY` = `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`), then `export DEBUG=false BL_TEST_REDIS_SERVER=C:/Users/Windows/bl-testenv/redis/redis-server.exe`. Local PG16 `bridgeleads_lookup1b_test` is at rev **110**; Redis db 13. Bare `python` is dead Anaconda. |
| Scratchpad (this session) | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/feececfd-3229-4469-bd0c-267cd19c6a24/scratchpad/`: <br>`mut_2d.py` (the 2d mutation runner); <br>`2d_files.txt` + `2d_chunk_00..08` (the 60-file regression list); <br>`codex_2d_*` prompts + `_out.txt`; <br>`mut_oc_i/ii/iii.py`, `mut_od.py`. |
| Prod checks (read-only, run from the OneDrive dir) | `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/<script>`: <br>`quiet.py` (the merge gate; read all 4 counts); <br>`lock_holders.py` (who holds jobs/results locks); <br>`oc_schema_check.py`, `oc_post_deploy_check.py`, `app_pending_priv.py`. |
| Deploy check | `railway deployment list --service {api,worker,beat} --json` (`[0].status`, `[0].meta.commitHash`), `railway logs --service worker`, `curl https://api.bridgeleads.io/health`. |

## 3. State
**LIVE in production this session (each merged under the gate, deploy verified):**

| PR | Merge | What |
|---|---|---|
| #421 | `bf792931` | migration 110: `skip_trace_queues.rows_sent`, `unmatched_billed` (schema only, shipped first) |
| #422 | `1a4076d4` | the dispatcher writes `rows_sent = len(claimed)` |
| #424 | `7a08c0a7` | billing rule `rows_sent>0 AND rows_uploaded>=rows_sent`; the decision persisted via `COALESCE … RETURNING` and billed BY it; the reconciler reads it (`billing_decision_unknown` flag) |
| #425 | `9bfc73b3` | O-D grant in 3 sources plus tests (executed-statement parsing, AST delete-site scan) |
| prod grant | applied | `verify_worker_delete_grants.py --apply` granted DELETE on `pending_skip_trace_rows` AND `skip_trace_cache` (pre-existing drift), owner-approved, verified read-only |

**Owner decisions (2026-10-01):**
- both grants;
- **AK2 accepted** (the table-wide worker DELETE; no RLS DELETE policy);
- start 2d;
- the BUILD_JOURNAL entry: yes (written, in #426).

**OPEN:**
- **#426** (`docs/lookup-od-merged`): the journal entry + the O-D records in the plan.
  Docs only, CI green, Codex GO. **NOT merged**, because `quiet.py` never showed all
  zeros: a `bridgeleads_system` read through Supavisor kept holding AccessShareLocks on
  jobs/results (1-7 locks, one transaction >30 s once). The rule needs all 4 counts at 0.
- **2d**: built, 32 tests pass, Codex diff review r1 NO-GO → all 5 findings fixed in
  `c840ea45`. NOT re-mutated, NOT regressed, NOT re-reviewed since.

## 4. Active files (2d, the 5-file PR; the handoff rides outside)
- `src/api/routes/jobs.py`, the confirm section starts at `:1315`:
  - `_PUBLISH_BOUND_S`, `_publish_slots` (`BoundedSemaphore(2)`), `_publish_pool` (2
    threads) `:1327`;
  - `_DELETE_IF_SAME_QUOTE` (Lua compare-and-delete) `:1335`;
  - `_valid_quote_payload` `:1344` (tz-aware expiry, list of UUIDs, int price > 0, 3-char
    currency, version 1-32 chars) → else 409 `quote_unsupported`;
  - `_action_for_quote` `:1378`, `_replayed` `:1389`, `_publish_blocking`,
    `_publish_contact_lookup` `:1408`;
  - the route `confirm_contact_lookups` `:1446`.
  - Flow:
    1. limiter (`writes`, 30/min, outer timeout → 503);
    2. job 404 / not done 409;
    3. **replay** by `(quote_id, user_id)` (must match job + category, else 409
       `quote_mismatch`) BEFORE the mutable gates (AO1);
    4. plan 402 / frozen / kill switch 503 / `paid_lookup_access`;
    5. Redis GET (missing / superseded / expired → 410 `quote_expired`; v≠2 → 409);
    6. payload validation; empty → 409 `nothing_to_look_up`;
    7. ONE transaction: the action INSERT, `INSERT…SELECT` quoted rows proven against
       `results` (user + job), count ≠ → 409 `quote_stale`, the event; the unique-quote
       `IntegrityError` → rollback, return the winner;
    8. after commit (never fails the request): compare-and-delete the quote key, a guarded
       import + bounded publish, then the conditional stamp `dispatched_at`
       (`status='dispatching' AND dispatched_at IS NULL`; zero rows = fine);
    9. `audit_log(..., user_id, ...)` → **202** `ContactLookupAction`.
- `src/api/schemas.py:2085+`: `ContactLookupConfirmRequest` (`quote_id`
  `^[A-Za-z0-9_-]{1,64}$`, category), `ContactLookupAction {action_id, status,
  quoted_count, truncated}`, `ContactLookupConfirmErrorDetail` / `Response`.
- `schema/openapi.json`: regenerated with `$PY scripts/export_openapi.py`. **0 deletions vs
  origin/main**, `--check` OK.
- `tests/test_contact_lookup_confirm.py` (32 tests). Real PG + Redis via the API client,
  the real quote endpoint, the real worker (`cla.run_action`). The `published` fixture is a
  PASS-THROUGH spy on `lookup_contacts.apply_async`. Labelled FAULT INJECTIONS:
  - a broker refusal;
  - the worker winning the stamp race;
  - a temp DB trigger refusing the stamp;
  - a hung publish;
  - a barrier spy on `_action_for_quote` forcing the unique-constraint race.
- `tasks/todo-lookup-contacts.md`: the spec, consults, "### 2d BUILT".
- Unchanged but relevant: `src/workers/contact_lookup_action.py` (2b worker; `run_action`,
  `lookup_contacts`), `src/workers/scheduler_helpers/contact_lookups.py` (P3 republish),
  `alembic/versions/101_contact_lookup_action_schema.py` (the guard triggers: what the API
  may write).

## 5. Changes made this session (beyond the PR table)
- The O-C was split schema-first into 3 PRs (Codex AE1): a new ORM column read by a worker
  on a stale schema would fail ingest and mark a PAID batch `errored`.
- Release gates written (`C:/Users/Windows/bl-checks/`): `oc_schema_check.py`,
  `oc_post_deploy_check.py`, `lock_holders.py`, `app_pending_priv.py`.
- **A bug found by mutation in 2d:** after the stamp's `rollback()`, `audit_log` read
  `current_user.id`, an expired ORM object → MissingGreenlet → a 500 after a committed
  purchase. Fixed (it uses the plain `user_id`) and pinned by the trigger test.
- Memory: `landmine_disk_full_zeroes_mutation_restore`,
  `landmine_skip_trace_cache_delete_grant_missing_in_prod`, `landmine_venv_schema_dead`,
  and the `project_lookup_1b1a_2026_09_25.md` update.

## 6. Failed attempts / traps (don't repeat)
- **The C: drive hit 0 bytes mid-mutation-run.** The restore write failed and 2 source files
  were zeroed. Recovered by `git checkout` + re-applying, matched by SHA-256. Check
  `df -h /c` first, COMMIT before mutating, `sha256sum -c` after any runner error.
- **My mutation runners lied:**
  - a mutant raised NameError (`text` not imported in the dispatcher), so it was "caught"
    by the wrong test;
  - heredocs ate `\n` escapes;
  - `site()` returned a duplicated tuple.

  Write runners with the Write tool; assert each mutant applied; read WHICH test failed;
  treat a timeout as INCONCLUSIVE.
- **A plan-edit script aborted on a wrong anchor** (`  injection` vs `  fault injection`),
  and I then ran a Codex round on the unfixed file. Check the script succeeded before
  consulting.
- **`.venv-schema` is dead** (Anaconda gone). Use `$PY` (fastapi 0.141.1 / pydantic 2.13.4
  = `requirements.txt`).
- **Never read ORM attributes after a rollback** in an async route.
- **`verify_worker_delete_grants.py --apply` grants EVERY missing table.** Report first.
- **A Codex premise was refuted with evidence** (AM1). Verify each premise in code.
- **A background regression was stopped** (TaskStop) when review fixes changed the code. No
  orphan pytest was left (checked with `Get-CimInstance … pytest`).
- **`quiet.py` connection can fail transiently:** re-run it. It prints counts and has no
  exit code: read all 4.

## 7. NEXT STEP (where I stopped) — on branch `feat/lookup-1b2d-confirm`
1. **Mutation re-run** (`mut_2d.py` in the scratchpad):
   - `sha256sum src/api/routes/jobs.py` first;
   - re-check every anchor (`c840ea45` changed the expiry/ids block and the publish call;
     the runner asserts anchors and will name the stale ones);
   - ADD mutants: each `_valid_quote_payload` check (tz, list, uuid, price, currency,
     version), the guarded post-commit import (remove the try), the 404/429 docs not
     needed;
   - slices well under 560 s; never wrap in `timeout`.
2. **Regression:** `2d_chunk_00..08` (60 files), each chunk to a file, foreground or one
   background job. Chunk 00 had passed 70 before the r1 fixes.
3. **Codex diff review r2:** `git diff docs/lookup-od-merged...feat/lookup-1b2d-confirm` →
   GATE: GO.
4. **Merge #426 first:** quiet all 4 zeros (re-check `lock_holders.py` if not), CI green on
   its exact head `be55f9f2`, main unchanged, `gh pr merge 426 --merge --match-head-commit
   be55f9f2…`. Announce "merging" / "verified" via SendMessage (ListAgents for names).
5. **Rebase 2d:** `git rebase --onto origin/main docs/lookup-od-merged`. Prove the 2d diff
   byte-identical (`diff <(git diff OLDBASE...OLDHEAD | grep -v '^index ') <(git diff
   origin/main...HEAD | grep -v '^index ')`), then a Codex re-check.
6. **Open the 2d PR** → CI green on the exact head → quiet → merge → api/worker/beat
   SUCCESS on the merge sha, `/health`, worker log. **Purchasable from here.**
7. Then **2e** (`GET /jobs/{id}/contact-lookups/{action_id}`: status, counts, the pause
   state), then the 1c frontend.
8. Follow-up (small, separate): `scripts/deactivate_test_batch_configs.py:3-5` and
   `scripts/purge_test_batch_configs.py:54-59` still say the system role has "DELETE=False
   on every table".

## 8. Rules (binding, from the owner)
- **Codex in the loop on every step, FOREGROUND:**
  - pre-code consult → `PLAN: GO`;
  - THREE-DOT diff review → `GATE: GO`;
  - after every rebase, a byte-identical diff proof + a Codex re-check.
  - Invocation: `codex exec "$(cat prompt)" -s read-only -c
    'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check < /dev/null >
    out 2>&1`. Open every prompt with "Do NOT load any skill, do NOT run /graphify or any
    preamble. Read-only: …" and tell it NOT to run pytest (shared test DB).
- **Tests:** real PG + Redis, no mocks. A pass-through spy is OK; fault injection only where
  unavoidable, and labelled.
- **A mutation runner per PR.** Regression in 7-8-file chunks, output to files. ruff clean;
  no type checker is configured (say so).
- **The 5-file rule** (the plan counts; a handoff rides outside). A merge is a deploy.
- **Standing merge rule:** `quiet.py` all 4 counts 0, CI green on the EXACT head, Codex GO,
  main unchanged, `--match-head-commit`. Never `--admin`. Coordinate with other sessions
  ("merging" / "verified").
- Before killing any process, confirm it is YOURS. Files are CRLF in the working copy:
  edit byte-safely and check for mixed endings.
- Ask the owner before any prod write; a BUILD_JOURNAL entry for 2d at the end (the owner
  said yes to journaling; ask where if unclear).
