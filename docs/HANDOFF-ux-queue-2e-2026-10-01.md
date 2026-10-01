# HANDOFF: UX audit queue, after 2d (2026-10-01)

Supersedes `docs/HANDOFF-ux-queue-2d-2026-09-30.md` (that one lives only on the unmerged branch
`docs/handoff-ux-queue-2d-2026-09-30`; you do not need it, everything still relevant is here).
Self-contained: read this first, then `CLAUDE.md` and memory `project_ux_audit_2026_09_26`.

## Goal (owner's standing request)
Work the BridgeLeads UX-audit follow-ups ONE AT A TIME, each end to end:

1. investigate;
2. write the plan in `tasks/todo-<item>.md`;
3. get Codex PLAN review until `PLAN: GO`;
4. **STOP: the owner confirms the plan**;
5. build with real-DB tests proven RED on unfixed code (or by a mutant, for a guard test);
6. run the full suite in 8 parts;
7. do the security review twice;
8. run the Codex diff review on `origin/main...HEAD` (THREE dots) until `GATE: PASS` (any P1 = no-go);
9. run the quiet check;
10. merge (merge IS deploy);
11. verify prod;
12. open the FE PR if the API changed;
13. write the BUILD_JOURNAL entry.

UI copy: no em dashes. Owner on merges/deploys: **"Just deploy and merge yourself i give you
permission"**.

Audit + ledgers live in the FE repo `docs/ux-audit/`: `UX-AUDIT.md` (49 findings) and
`phase-3.0-contracts.md` (contracts Q1-Q6; Q1, Q2, Q5 and Q6 are now marked SHIPPED there).

## Where you are: NOTHING IN FLIGHT
2d is fully closed, live and prod-verified. There is no open feature branch of mine. The next
item is **2e (Q4)**; no branch or worktree exists for it yet.

| Item | PR | Squash | State |
|---|---|---|---|
| 2d BE (notification `already_delivered`, API docs) | BE #405 | `4c45ac06` | live 01:23Z 10-01 |
| 2d FE ("0 new · 123 already delivered") | FE #172 | `4c50eb8b` | Vercel prod 02:16Z |
| 2d journal | BE #412 | `1108d554` | merged 02:47Z, redeploy verified |
| 2c + 2d prod check | job `4b092852` | n/a | PASSED 03:27Z (below) |

**Prod verification (done, owner-approved real run):**
- The job: `4b092852` on scraper "Gir" (config `f3566c47`, King pre-foreclosure, skip-trace off,
  account `b6d2095d`, Agency unlimited). Inserted `pending` at 03:08:21Z; the watchdog started it
  at 03:23:21Z; done at 03:26:35Z.
- **2c:** found 7 = dropped 0 + no_address 0 + same_run 0 + already_delivered 1 + over_quota 0 +
  new 6, and new = record_count = billed_count = 6. It reconciles.
- **2d:** the `job_completed` notification detail has `already_delivered: 1`, equal to
  `breakdown_already_delivered`. It matches.

Main heads at handoff time: BE `origin/main` = `48f88663` (#411 lookup 1b-2b, by another session),
**alembic head = 108** (`108_scraper_mode_template.py`, by the AI-removal session). FE
`origin/master` = `1f36937` (#175).

## What 2d changed (code you are standing on)
- `src/workers/tasks_helpers/finalize.py`: new `emit_job_completed(job, config, job_id,
  display_count)`. It builds the `job_completed` detail (scraper_name, county, record_count, plus
  `already_delivered` from `breakdown_from_job(job)` when a valid snapshot exists: present incl. 0,
  absent when unknown) and calls `create_notification`. `run_scrape_job` (`src/workers/tasks.py`)
  calls it once after `FinalizeKind.DONE`.
- `src/api/schemas.py`: `JobResponse.breakdown` / `ResultsPage.breakdown` gain `Field(description)`
  (account-wide, raw, snapshot-only on the list). `GET /jobs` is SNAPSHOT-ONLY by decision: the
  shell polls it every 5 s, and the narrowest live count measured p95 9.9 s for the largest account.
- Tests: `tests/test_completion_notification.py` (new), list guards in
  `tests/test_run_breakdown_api.py`, the post-DONE source guard in `tests/test_finalize_fence.py`.
- FE: `components/run-lead-count.tsx` (`RunLeadCount`, `alreadyDeliveredOf` = snapshot basis
  only; a `stacked` two-line form for narrow cells), used by the Results index, the dashboard
  `ScrapersTable` and `NotificationsBell`.
- Plan + review: BE `tasks/todo-2d-jobs-already-delivered.md`. Journal: top entries of
  `docs/BUILD_JOURNAL.md`.

## NEXT STEP (exact): start 2e = Q4 (F-009) "Lookup failed"
Contract: FE `docs/ux-audit/phase-3.0-contracts.md` "Q4". Re-checked on 2026-10-01:
- **Still a bug on FE master.** `app/(dashboard)/results/[id]/_components/EmailCell.tsx` has NO
  `errored` branch: an errored row falls through to "None found" (line ~40).
  `PhoneCell.tsx:142-145` renders `errored` as "Error".
- **Backend:** `results.last_trace_outcome` (migration 101) is STILL written by nothing on main
  (only a comment in `src/api/contact_lookup_planner.py:134`). The contract says the row-level
  `skip_trace_status` is enough for the display fix; `last_trace_outcome` is a LATER backend step
  (not_submitted / provider_error / retries_exhausted / charged_unmatched).
- Contract table, per channel from `(skip_trace_status, list)`:
  - `queued`/`submitted` -> "Processing";
  - `errored` -> "Lookup failed";
  - `hit`/`miss` with a non-empty list -> the values;
  - `hit`/`miss` with an empty list -> "None found";
  - `not_attempted` -> "Not looked up".
- Open question from the contract (ask the owner in the plan): may "Lookup failed" offer a retry?
  The contract says NOT until `last_trace_outcome` can rule out re-buying a charged lookup.
- Likely FE-only, so no BE PR, no Railway deploy, no OpenAPI change. Check every other surface that
  renders contact status: `LeadCards.tsx` (mobile), `BatchLeadsTable.tsx`, any skip-trace chip,
  and the "already delivered" contacts summary (`AlreadyDeliveredContacts.failed` exists on the
  BE). Also look at the contact-lookup work other sessions merged (#406, #411: lookup 1b-2a/1b-2b,
  "pending rows name their action, unmatched_unbilled") for new statuses the cells must handle.
  Do not assume the set of statuses is still the six above: read `src/db/models.py` around
  `skip_trace_status` and the 1b-2 plan in BE `tasks/`.

**How to start:**
1. FE worktree. `C:/Users/Windows/bl-fe-breakdown` has `node_modules`; it is on my merged branch
   `feat/already-delivered-2d`. In it:
   `git fetch origin && git switch -c feat/lookup-failed-2e origin/master`.
   Never delete or force-move branches in the shared OneDrive checkout.
2. A BE worktree only if a BE change turns out to be needed:
   `git worktree add -b feat/<name> C:/Users/Windows/bl-wt/<dir> origin/main`.
3. Write `tasks/todo-2e-lookup-failed.md`, in the BE repo if anything there changes. FE-only work
   has no `tasks/`; then put the plan in the FE repo's `docs/ux-audit/` beside the contracts, or
   ask the owner.
4. Brainstorm, get the Codex PLAN review until GO, then STOP and ask the owner to confirm.

## Environment / reusable commands
- **Test env:** `source C:/Users/Windows/bl-testenv/env-eligibility.sh`. It sets `$PY` and pins
  `DATABASE_URL*` to DB `bridgeleads_eligibility_test` and Redis to db 7. **Never bare pytest**:
  the repo `.env` is PROD.
  - The test DB is at **107**. Main is at **108**, so before BE tests run
    `unset DATABASE_URL_MIGRATE; "$PY" scripts/migrate.py` from a worktree on current main, after
    sourcing the env. `alembic/env.py` refuses any target that is not `TEST_DATABASE_URL_SYNC`.
- **Full suite, 8 parts, ONE AT A TIME** (RAM is tight), ONE BACKGROUND JOB PER PART so a reap
  loses one part only:
  - setup: `ls tests/test_*.py | sort > all.txt; split -n l/8 all.txt part_`;
  - per part: `"$PY" -m pytest $(cat part_xx) -q -p no:cacheprovider > part_xx.log 2>&1;
    echo $? > part_xx.exit`;
  - read the exit file, never through `| tail`.
  - Known local-only failure: `test_contact_lookup_quote.py::test_a_quote_redis_cannot_store_is_never_shown`
    (needs Redis ACLs; green in CI). A full run takes ~65 min.
- **Codex** (never `codex review`: it runs pytest on your DB):
  - build a prompt file (preamble + `git diff origin/main...HEAD`; tell it "do not run tests" and
    "do not load UX critique skills", because it once wandered into a design-critique skill);
  - run it from an empty dir:
    `codex exec - -c 'mcp_servers={}' --skip-git-repo-check -s read-only -C <worktree> < prompt.txt > out.txt`;
  - the verdict is the text after the LAST line equal to `codex`. WAIT ON THE PROCESS EXIT: a
    loop grepping for "PLAN: GO" matched the echoed prompt once.
- **FE gates:**
  - `npx --no-install tsc --noEmit`, `npx --no-install eslint <files>`, `npm run build`.
  - Types drift gate: regenerate from BE main with
    `git -C <BE wt> show origin/main:schema/openapi.json > tmp.json;
    npx --no-install openapi-typescript tmp.json -o lib/api-types.generated.ts`. Description-only
    BE changes move it too.
- **FE proof without a test runner:**
  - stub API + Playwright, reusable from my scratchpad `fe2d/ad_stub.mjs` + `ad_verify.mjs`
    (session 81fad222, under `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/81fad222-0822-4e99-97c0-9859da4c87da/scratchpad/`);
  - `.env.local` (gitignored, create then DELETE): `NEXT_PUBLIC_API_URL=http://127.0.0.1:8123`, a
    random `AUTH_SECRET`, `AUTH_URL`/`NEXTAUTH_URL=http://127.0.0.1:3111`, `AUTH_TRUST_HOST=true`;
  - `npm run build`, then `npx next start -p 3111 -H 127.0.0.1`;
  - `TaskStop` leaves the `next` server alive: kill the 3111/8123 listener only after checking its
    command line is yours (PowerShell: `"${p}:"`, not `"$p:"`, inside strings);
  - the stub `/auth/onboarding` must return a full `next_action`;
  - at desktop width, wait on `tr` hasText, because hidden mobile cards hold the same names.
- **Quiet check before ANY BE merge** (merge = deploy; Railway runs migrations on boot). Run it from
  the OneDrive repo dir, which is railway-linked (worktrees are not):
  `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe C:/Users/Windows/bl-checks/quiet.py`
  -> all four counts must be 0.
  - A non-zero "other sessions in a transaction > 30s" must be IDENTIFIED (pg_stat_activity +
    pg_locks), never rounded to zero. A crashed check is NOT quiet.
- **Merge:** `gh pr merge N --squash --match-head-commit "$(git rev-parse HEAD)"` (never
  `--admin`). Branch protection is strict, and `Test` + `Dependency Audit` are REQUIRED.
- **Verify a deploy:**
  - `railway status --json` (api/worker/beat commitHash = merge SHA, SUCCESS);
  - `curl https://api.bridgeleads.io/health`;
  - `railway logs --service worker` shows "ready" and no errors;
  - FE: `gh api repos/Abenezer1244/bridgeleads-web/commits/<sha>/status` (Vercel success).
- **If `railway` says "Unauthorized":** ask the owner to run `! railway login`.
- **Prod read-only checks** (scratchpad, session 81fad222): `check_2c_2d.py` (snapshot reconcile +
  notification match), `first_snap.py`, `candidates.py` (active scrapers + last run stats).
- **A real prod run ONLY with explicit owner OK:** INSERT a `jobs` row (`id`, `user_id`,
  `scraper_config_id`, `status='pending'`, `trigger='manual'`) and the watchdog picks it up in ~15
  min. Pattern in `fire_gir.py` / `watch_job.py`. Ask the other sessions to hold merges first:
  a redeploy kills an in-flight job.

## Other sessions (coordinate the merge slot)
Several sessions merge to BE main and FE master within the hour. Use `ListAgents` +
`SendMessage`; before merging or firing a prod run, ask them to hold, and tell them when a deploy
is verified. Seen on 10-01:
- `web-scrapper-automation-30`: AI-mode removal. Its 2c draft cannot merge before 2026-10-08.
- `web-scrapper-automation-38`: contact lookup 1b-2 (#406, #411).
- `web-scrapper-automation-f3`: unknown, did not reply.

When FE types drift, whoever merges an FE PR second regenerates them from BE main.

## Failed attempts / landmines (don't repeat)
- 🛑 The Codex wait loop matched "PLAN: GO" in the ECHOED PROMPT and returned instantly. Wait on the
  process exit, then read after the last `codex` line.
- 🛑 The low-memory reaper killed the full suite (after 2 of 8 parts) AND a prod-job watcher. Never
  restart a reaped job on your own: report it and ask. One background job per suite part.
  A reaped pytest child can SURVIVE its shell: find it by command line (`Win32_Process`), confirm
  it is yours, and never kill another session's (one ran under `timeout 570` with a different
  file list).
- 🛑 `main` turned red for every PR on 09-30 at 16:24Z (new pyjwt CVE; Dependency Audit is
  required). Check main's own CI before blaming your branch. It was fixed by #407.
- 🛑 Rebasing under a running pytest is unsafe: stop your suite first.
- 🛑 After a rebase, prove the patch unchanged:
  `diff <(grep -v '^index \|^@@' pre.diff) <(grep -v '^index \|^@@' post.diff)` = 0 lines. If
  main brought a migration, upgrade the test DB and re-run the affected tests (CI runs the full
  suite on the merge).
- 🛑 Two sessions' journal entries both insert at the top, so the rebase conflicts. Reset to main
  and re-insert the reviewed entry verbatim, rather than resolving commit by commit.
- 🛑 The Codex journal fact-check fails on any number without a recorded source (it failed twice
  on "122 done runs"). Cite the query or drop the number.
- 🛑 An FE inline "a · b" in a flex-wrap wraps to a line starting with "·": use a stacked form in
  narrow cells.
- 🛑 Never hand-type a SHA (use `git rev-parse`). After each commit,
  `git status --short | grep -v '^??'` must be empty.
- 🛑 Python patch scripts: preserve CRLF (detect `\r\n`). Printing non-ASCII to the Windows console
  raises cp1252 `UnicodeEncodeError`: use the Edit tool, or `sys.stdout.reconfigure(encoding="utf-8")`.
- 🛑 `test_db_safety` under memory pressure: 17 false fails with 0xC0000142; re-run the file alone.

## Leftovers to clean up (safe, mine)
- Worktree `C:/Users/Windows/bl-wt/delivered2d` (branch `feat/jobs-already-delivered-2d`, merged
  as #405): `git worktree remove` it once you no longer need it. Keep the remote branch (owner
  rule: no branch deletes in the shared checkout).
- FE worktree `C:/Users/Windows/bl-fe-breakdown` is on merged `feat/already-delivered-2d`; switch
  it to the new 2e branch (step 1 above).

## Later queue (after 2e)
- Item 3: batches B-E (+ F-045..F-050).
- Item 4: F-043 Phase 2 (skipped status, batch pause/delete, deleted-while-paused prod rows).
- Item 5: Phase 4/5.

Queued follow-ups, owner-visible, not started:
- stale-attempt log-publish races (owner-accepted P2s);
- an attempt-unique export key;
- widen the fence to plan-cap/enrichment writes;
- a `connector_unavailable` refusal;
- a `jobs.was_ai` snapshot;
- `/scrapers` label overlap;
- `test_rls_isolation` role GRANTs;
- the `AnimatedCounter` screen-reader value (FE);
- a Railway CLI upgrade.
