# HANDOFF: contact lookup Phase 1b-2, after 2c. Next is O-C (the billing decision), 2026-10-01

Read this whole file first. Then read these sections of `tasks/todo-lookup-contacts.md` (search
the headings). Together they are the NORMATIVE contract.
- **"## Phase 1b-2 — the WRITE path"**: Facts, S1-S3, steps 2a-2e, consults r1-r5
  (V1-V8, W1-W7, X1-X3), OWNER DECISIONS (2026-09-30), "Owner items" (O-A, O-B, **O-C**).
- **"### 2b BUILD SPEC" through "### 2b MERGED + LIVE"**, including **OWNER DECISION O-D**.
- **"## Phase 1b-2c — the reconciler"**, consult r1 (**AA1-AA9**, and especially **AA2**, which
  GROWS O-C), r2 (AB1-AB4), "2c-i BUILT", "2c-ii BUILT", and the two MERGED records.

Where these amend earlier text, the later amendment wins.

## The goal
Phase 1 of contact lookup: a customer buys skip-trace (contact) lookups for the leads on a results
tab. The provider is Tracerfy, and the operator pays per credit (normal 1, advanced 2).

1b-2 is the write path:
1. a customer confirms a quote;
2. a durable action row is created;
3. a worker claims the quoted leads into the paid queue;
4. a reconciler re-drives, expires and settles actions.

The design adds NO new spend path. Leads enter the queue only through
`lock_job_for_claim()` + `claim_skip_trace_rows()`. **Settlement is bookkeeping, never billing:
billing stays per pending row at ingest.**

## Where things are
| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup`. **NEVER the OneDrive checkout: its `.env` is PRODUCTION.** |
| Branch to continue on | **`feat/lookup-oc-billing-decision`**, off main `f2fe1573`. LOCAL; it holds only this handoff + the plan's 2c-i/2c-ii MERGED records. Build O-C here. |
| Test env | `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY`), then `export DEBUG=false`. Local PG16 `bridgeleads_lookup1b_test` is at **rev 109**. Local Redis 5.0.14 db 13. **Bare `python` is a dead Anaconda: always `$PY`.** |
| Store-failure test | set `BL_TEST_REDIS_SERVER=C:/Users/Windows/bl-testenv/redis/redis-server.exe` for `tests/test_contact_lookup_quote.py`. |
| Local migrate | `$PY scripts/migrate.py` with the env sourced. Run it after ANY migration lands on main. |
| Prod checks | From the OneDrive dir: `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python C:/Users/Windows/bl-checks/quiet.py`. Railway auth was renewed 10-01; if it says "Unauthorized", ask the owner to run `! railway login`. |
| Deploy check | `railway deployment list --service {api,worker,beat} --json` (`[0].status`, `[0].meta.commitHash`), `railway logs --service worker`, `curl https://api.bridgeleads.io/health`. |
| Scratchpad (last session) | `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/4ebfb689-6052-439b-9ea8-adfb4e7e1f46/scratchpad/`: `codex_2b_*`, `codex_2c_*`, `codex_2ci_*`, `codex_2cii_*` prompts + `_out.txt`; mutation runners `mut2b.py`, `mut2ci.py`, `mut2cii_v2.py` (**v2 is the pattern to copy**: slices, a baseline guard, the mutant asserted present, restore checked by content); `priv_check.py` (the read-only prod grant check). |

## State: LIVE in production (each merged under the gate, deploy verified)
| PR | Merge | What |
|---|---|---|
| #400 | `2b907bc1` | 2-0: the repair script serialises with the claim |
| #402 | `e9397f0f` | 2a-i: migration 107 (action FK on pending rows, `unmatched_unbilled`, `quote_snapshot`) |
| #406 | `8c9afb98` | 2a-ii: the claim writes `action_id`, reports `held_ids` |
| **#411** | `48f88663` | **2b**: the worker `lookup_contacts(action_id)` in `src/workers/contact_lookup_action.py`, and the enrich extraction (`settle_charged_unanswered`, `copy_cached_answer`) |
| **#416** | `c517cef7` | **2c-i**: `ACTION_DEADLINE_SECONDS` enforced in T1's CAS; `_flag()` + `FLAGGABLE`; `queue_accepted_all()` extracted in `src/api/billing/skip_trace_usage.py` |
| **#419** | `f2fe1573` | **2c-ii**: the reconciler `src/workers/scheduler_helpers/contact_lookups.py`, beat `reconcile-contact-lookups` every 120 s. The first prod run succeeded, all zeros. |

**Everything is IDLE until 2d:** no code creates an action yet.

**NOT STARTED:**
1. **O-C**: billing, a HARD GATE before 2d;
2. **O-D**: the grant, a HARD GATE before 2d;
3. **2d**: the confirm endpoint (**ASK THE OWNER BEFORE STARTING 2d**);
4. **2e**: the GET status endpoint.

## Active files (what each one is)
- `src/workers/contact_lookup_action.py`: the ONE state machine.
  - `ACTION_TRANSITIONS`, `VERDICT_TRANSITIONS`, `FLAGGABLE`;
  - `_move`, `_set_verdicts`, `_flag`, `_quoted_ids`;
  - T1/T2 (`run_action`, `_claim`), `lookup_contacts`, `ACTION_DEADLINE_SECONDS`, `LEASE_SECONDS`.
- `src/workers/scheduler_helpers/contact_lookups.py`: the reconciler.
  - P1 expire, P2 lease take-back, P3 re-publish, P4 settle.
  - `_CURRENT_ROW` (a LATERAL, active-first), the blocker predicates, `_candidates` (SKIP LOCKED
    before LIMIT), `_each` (per-action isolation).
  - **P4 maps `unmatched` with the LIVE recomputed `queue_accepted_all`: this is what O-C changes.**
- `src/api/billing/skip_trace_usage.py`: `queue_accepted_all()` (`:537`); `report_usage_from_webhook()`
  (`:563`, calls it at `:640`).
- `src/workers/skip_trace_dispatcher.py`: `_persist_submission` (`:1475`, writes
  `rows_uploaded=response.get("rows_uploaded") or len(claimed)` at `:1533`); `_release_claim`;
  the stale-claim reconciler `_reconcile_stale_claims` (`~:1961`). The adoption path rewrites
  `rows_uploaded` at `~:2104, 2157, 2222`.
- `src/workers/tracerfy_ingest.py`: writes `skip_trace_queues.rows_uploaded` (`~:1004`) THEN calls
  billing (`~:1024`), in ONE transaction.
- `src/db/models.py:1315`: `SkipTraceQueue`.
- Tests:
  - `tests/test_contact_lookup_action.py` (2b + 2c-i);
  - `tests/test_contact_lookup_reconciler.py` (2c-ii, 27);
  - `tests/test_tracerfy_ingest.py`: pins billing end to end (`test_unmatched_row_IS_billed`,
    `test_provider_dropped_row_is_NOT_billed`,
    `test_dedup_shrinking_the_upload_also_suppresses_unmatched_billing`) plus `queue_accepted_all`.

## NEXT STEP: O-C, as grown by AA2 (branch `feat/lookup-oc-billing-decision`)
**The owner's O-C (2026-09-30):** persist `skip_trace_queues.rows_sent` at submission. Billing's
`accepted_all` compares `rows_uploaded` with it, and bills `completed` only on a mismatch OR a NULL
`rows_sent` (legacy rows). Its own PR, BEFORE 2d. Regression tests are required for
`rows_sent != rows_uploaded` and for `rows_sent IS NULL`.

**Why: a pre-existing live billing gap (W3).**
- The rows actually SENT (`len(claimed)` in `_persist_submission`) are never persisted.
- `accepted_all` compares `rows_uploaded` with the rows STAMPED with the queue id.
- `_persist_submission` allows `moved < claimed` (alert only).
- So in a partial-bookkeeping batch, unmatched rows can be billed although a row was dropped.

**AA2 adds:** the reconciler must read the decision billing ACTUALLY made, not recompute it later.
The queue row and the stamped count can change after billing ran (adoption / redrive rewrite
`rows_uploaded`; partial bookkeeping). So O-C also:
1. **persists billing's decision per queue**, e.g. a nullable `skip_trace_queues` boolean written
   ONCE by `report_usage_from_webhook` in ingest's transaction;
2. **switches the reconciler's P4 `unmatched` mapping to read that column**, instead of calling
   `queue_accepted_all`. Decide what a NULL means (a queue billed before O-C). No action can
   predate O-C, because 2d is gated on it; confirm that in the consult.

**Before code, per the rules:**
1. read main's diff against the plan facts;
2. write the O-C spec in the plan (a migration of **110**, after `git fetch` and a check of the
   alembic head; the dispatcher; billing; the reconciler; tests);
3. split under the 5-file rule. It will be 2 PRs, e.g.:
   - O-C-i: migration + `models.py` + its tests + the plan;
   - O-C-ii: dispatcher + billing + reconciler + tests + the plan;
4. **run a Codex pre-code consult until `PLAN: GO`**.

This is a LIVE billing change, so quiesce thinking applies: a merge is a deploy, and the
migration runs on boot.

**Then O-D (owner chose GRANT, 2026-10-01):** `GRANT DELETE ON pending_skip_trace_rows TO
bridgeleads_system` in ALL three sources:
- `scripts/provision_rls_roles.sql`;
- `scripts/verify_worker_delete_grants.py`;
- `scripts/_cutover_step2_grants_policies.py` (`_SYSTEM_DELETE_TABLES`).

Applying it in production is a prod change: confirm with the owner before running it. Verify
with `priv_check.py`. **Confirmed missing in prod 10-01** (the worker role has SELECT, INSERT,
UPDATE only). Effect today: the claim's lost-race withdrawal raises and rolls back the whole
claim, on the live enqueue too. It costs availability, never money.

**Then 2d (ASK THE OWNER FIRST).** Then 2e. Then a BUILD_JOURNAL entry: **ask the owner where,
and whether, before writing it** (still unanswered).

## Rules (binding, from the owner)
- **Codex in the loop on every step, FOREGROUND.**
  - Pre-code consult until `PLAN: GO`.
  - Review the THREE-DOT diff (`origin/main...HEAD`) until `GATE: GO`.
  - After EVERY fetch + rebase: prove the diff is byte-identical
    (`diff <(git diff OLDBASE...OLDHEAD | grep -v '^index ') <(git diff origin/main...HEAD | grep -v '^index ')`)
    and get a Codex re-check.
  - Invocation: `codex exec "$(cat prompt.txt)" -c 'model_reasoning_effort="high"' -c 'mcp_servers={}' --skip-git-repo-check < /dev/null > out.txt 2>&1`.
    Open every prompt with "Do NOT load any skill, do NOT run /graphify or any preamble.
    Read-only: …".
- Real PG + Redis tests, no mocks (a pass-through spy is OK; fault injection only where
  unavoidable, and labelled).
- A mutation runner per PR.
- Regression over every test file touching the area, in chunks of 7-8 files (each under ~8 min),
  output to a file.
- ruff clean. No type checker is configured: say so.
- **A merge is a deploy.** Keep the 5-file rule (the plan file counts; a handoff rides outside).
- **Standing merge rule:** merge your own PR when ALL hold:
  - `quiet.py` exits 0 with all four counts at 0;
  - CI is green on the EXACT head;
  - Codex says GO;
  - main is unchanged since the rebase;
  - you merge with `gh pr merge N --merge --match-head-commit <sha>`.

  **Never `--admin`.** Then verify the deploy (all three services SUCCESS on the merge sha, worker
  log, `/health`).
- **Coordinate merge slots** with the other local sessions via SendMessage:
  - `web-scrapper-automation-30` (security / AI-mode work; it said it has nothing further today);
  - `web-scrapper-automation-1c` (the UX queue; #420 was taking the slot at handoff).

  Protocol: announce "merging", then "verified". First green merges, and the other rebases. Use
  `ListAgents` to find the current names.
- Before killing any process, confirm it is YOURS.
- Ask the owner before writing a BUILD_JOURNAL entry and before starting 2d.

## Failed attempts / traps hit (don't repeat)
- **A mutation runner killed by an outer `timeout` skipped its `finally`** and left a LIVE mutant
  (an inverted billing rule) in `skip_trace_usage.py`. `git diff --stat` looked clean; a content
  check caught it. Run in slices well under 600 s, never wrap a runner in `timeout`, and verify by
  content. Memory: `landmine_killed_mutation_runner_leaves_the_mutant`.
- **A RED baseline makes every mutant "caught".** A test broke mid-session, and a batch of
  "CAUGHT"s was void. The v2 runner refuses a red baseline: keep that.
- **A mutant that "survives" can be a test passing for the wrong reason.** Example: the matrix
  test raised from the lease rule, not the matrix. Assert the specific message.
- **Group-selection logic hid a real gap that only a test found:** a missing-row blocker with
  another lead in flight was never visited. Codex then found three more fairness bugs in review
  (a locked prefix, per-action isolation, selection-vs-visit row choice). Selection and visit must
  share ONE definition.
- **Codex's premises can be false** (r1 claimed billing reads `rows_uploaded` before ingest
  writes it; wrong). Verify each premise in code, and keep the conclusion only if it still holds.
- **Rejected with reasoning, and Codex agreed:** an atomic predicate re-check inside the claim
  INSERT. The money boundary is submission (`_partition_still_deliverable` locks FOR SHARE and
  re-checks).
- **main moved under almost every PR** (#408, #409, #410, #412, #413, #414, #417, #418). Rebase,
  prove the diff identical, Codex re-check, and re-run CI (~22 min) every time.
- **Background CI watchers get killed for low memory.** Poll in the FOREGROUND with an
  `until … do sleep 45; done` loop under ~560 s, and don't restart killed watchers.
- **Files are CRLF in the working copy and LF in the index.** Appending with a heredoc makes a
  mixed file: edit with the Edit tool, or byte-level Python using the file's own newline.
- **`results.skip_trace_status` and `skip_trace_queues.rows_uploaded` are NOT NULL**: no NULL
  test cases for them.
- **The beat schedule is set only when `src.workers.scheduler` is imported**: a test must import it.
- **`railway run` was "Unauthorized"** until the owner re-ran `railway login`.

## Paste-ready prompt for the next session
See the bottom of the previous session's final message. It is also reproduced here:

```
Continue the BridgeLeads contact-lookup work: Phase 1b-2, next step O-C (the billing
decision fix, grown by consult AA2). Work in the worktree C:/Users/Windows/bl-wt-lookup on
branch feat/lookup-oc-billing-decision (LOCAL, off main f2fe1573; it holds only the handoff +
plan records). Do NOT use the OneDrive checkout: its .env is production.

FIRST read in full, in order:
  1. C:/Users/Windows/bl-wt-lookup/docs/HANDOFF-lookup-1b2-oc-2026-10-01.md
  2. In tasks/todo-lookup-contacts.md: "## Phase 1b-2 — the WRITE path" (Facts, steps,
     V/W/X consults, OWNER DECISIONS, Owner items O-A/O-B/O-C), "### 2b MERGED + LIVE"
     (O-D decision), "## Phase 1b-2c — the reconciler" with consult r1 (AA1-AA9, esp. AA2)
     and r2 (AB1-AB4), and the 2c-i / 2c-ii BUILT + MERGED records.

State: 2b (#411), 2c-i (#416), 2c-ii (#419) are LIVE and idle until 2d. Next: O-C as grown
by AA2 (persist rows_sent AND billing's per-queue decision; billing bills unmatched only when
rows_uploaded >= rows_sent; the reconciler reads the persisted decision instead of
recomputing queue_accepted_all), migration 110, split into 2 PRs under the 5-file rule.
Then O-D (GRANT DELETE ON pending_skip_trace_rows TO bridgeleads_system in all 3 grant
sources; confirm with me before applying in prod). Then ask me before 2d.

Rules: Codex in the loop on every step, FOREGROUND (pre-code consult to PLAN: GO, three-dot
diff review to GATE: GO, re-check after every rebase with a byte-identical diff proof). Real
PG + Redis tests, no mocks; mutation runner per PR (sliced, baseline guard, verify restore by
content); regression in 7-8 file chunks, output to a file. A merge is a deploy; keep the
5-file rule. You may merge yourself under my standing rule: quiet.py exit 0 with all four
counts 0, CI green on the exact head, Codex GO, main unchanged, --match-head-commit. Never
--admin. Coordinate merge slots with the other local sessions via SendMessage ("merging" /
"verified"). Before killing any process, confirm it is YOURS. Ask me before writing a
BUILD_JOURNAL entry and before starting 2d.
```
