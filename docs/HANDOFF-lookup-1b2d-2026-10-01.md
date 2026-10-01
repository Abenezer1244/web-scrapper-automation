# HANDOFF: contact lookup 1b-2d (the confirm endpoint), 2026-10-01

Read this file, then in `tasks/todo-lookup-contacts.md`:
- **"## Phase 1b-2d — the confirm endpoint"** (the spec, consult r1-r7, AO1-AO5);
- **"### 2d BUILT"** (the state and NEXT steps).

## State
- **LIVE:**
  - O-C: #421 (migration 110), #422 (`rows_sent`), #424 (the billing rule + the
    persisted decision);
  - O-D: #425, plus the prod grant APPLIED (DELETE on `pending_skip_trace_rows` and
    `skip_trace_cache`, owner-approved, verified read-only).
- **Owner decisions (2026-10-01):** apply both grants; **AK2 accepted** (the table-wide
  worker DELETE, no RLS policy); **start 2d**; a journal entry: yes.
- **#426 (docs: the journal entry + the O-D records): OPEN, NOT MERGED.** CI green (docs
  only). It waited on `quiet.py` all zeros: another `bridgeleads_system` read through
  Supavisor held shared locks on jobs/results (`C:/Users/Windows/bl-checks/lock_holders.py`
  shows them). Merge it first, under the standing rule.
- **2d: branch `feat/lookup-1b2d-confirm` (pushed), stacked on #426's branch, NO PR yet.**
  5 files: `jobs.py`, `schemas.py`, `openapi.json`, `tests/test_contact_lookup_confirm.py`,
  the plan. 32 tests pass. Codex PLAN: GO (r7). Diff review r1 NO-GO, all fixed in
  `c840ea45`.

## Next, in order
1. The mutation runner `mut_2d.py`
   (`C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/feececfd-3229-4469-bd0c-267cd19c6a24/scratchpad/`):
   - re-check every anchor (r1 changed the code);
   - add mutants for `_valid_quote_payload` (tz, list, uuid, price), the guarded post-commit
     import, and the 404/429 docs;
   - run in slices (<560 s); verify the hash after each.
2. The regression: `2d_chunk_00..08` in that scratchpad (60 files). Run chunks in the
   FOREGROUND or one background job, output per chunk.
3. Codex diff review r2: `git diff docs/lookup-od-merged...feat/lookup-1b2d-confirm`.
4. Merge #426 (quiet all zeros, `--match-head-commit`), then `git rebase --onto origin/main
   docs/lookup-od-merged` on the 2d branch, prove the 2d diff byte-identical, Codex re-check.
5. Open the 2d PR → CI green on the exact head → quiet → merge → api/worker/beat SUCCESS +
   `/health` + the worker log. **This makes lookups purchasable.**
6. Then 2e (`GET /jobs/{id}/contact-lookups/{action_id}`), and the 1c frontend.

## Traps hit this session (don't repeat)
- **The C: drive filled to 0 bytes** and zeroed 2 source files mid-mutation-run
  (restore-write ENOSPC). Check `df -h /c` first; commit before mutating; `sha256sum -c`
  after any runner error.
- **My runners lied twice:** a mutant raising NameError (an unimported name), and heredocs
  eating `\n` escapes. Write runners with the Write tool; assert each mutant applied; read
  the FAILED test name.
- **`.venv-schema` is dead** (Anaconda gone). The working venv (`$PY`) pins match
  `requirements.txt` (fastapi 0.141.1 / pydantic 2.13.4): regenerate the OpenAPI with it,
  0 deletions vs `origin/main`, and `--check` OK.
- **A rollback in an async route expires ORM objects:** never read `current_user.X` / `job.X`
  after one (MissingGreenlet). Capture plain values first.
- **`verify_worker_delete_grants.py --apply` grants EVERY missing table.** Run the report
  first.

## Rules (unchanged, binding)
Codex FOREGROUND on every step (consult → GO, three-dot diff review → GO, a re-check after
every rebase with a byte-identical proof). Real PG + Redis, no mocks (a pass-through spy is
OK; fault injection only where unavoidable, and labelled). The 5-file rule (this handoff
rides outside). A merge is a deploy. Merge only when quiet has all 4 counts at 0, CI is
green on the exact head, Codex says GO, main is unchanged, with `--match-head-commit`;
never `--admin`. Announce "merging" / "verified" to the sessions in `ListAgents`.
