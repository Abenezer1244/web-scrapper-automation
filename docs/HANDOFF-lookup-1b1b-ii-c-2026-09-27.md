# HANDOFF — contact lookup, Phase 1b-1b-ii-c (keyset refill), 2026-09-27

Read this whole file before touching anything. Then read, in order:
1. `tasks/todo-lookup-contacts.md` — sections **"Phase 1b-1b-ii-c — the keyset frontier"** through
   **"ii-c TO BUILD"** (the consult rounds F1-F7, H1-H3 are binding), plus the Deferred section's
   first bullet (the env.py safety hazard).
2. `src/workers/skip_trace_capacity.py` and the refill loop in
   `src/workers/skip_trace_dispatcher.py` `dispatch_pending_skip_trace` (search `REFILL`).

## Where things are

| Where | What |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-lookup` (NOT the OneDrive checkout) |
| Branch | `feat/lookup-1b1b-ii-c2-keyset-allocate` @ `99b3c8cd` (WIP) + this handoff commit. **Local only, never pushed.** Stacked on ii-c-1, which is now in `main`. |
| Test DB | local PG16 `bridgeleads_lookup1b_test`, env `source C:/Users/Windows/bl-testenv/env-lookup1b.sh` (gives `$PY`). Start PG/Redis/6543 proxy per memory `reference_local_full_pytest_2026_07_03` if down (`pg_ctl -D C:/Users/Windows/bl-testenv/data -l .../pg_claude.log start`). Test DB is at revision **103**. |
| Prod checks | read-only scripts in `C:/Users/Windows/bl-checks/`: `quiet.py` (pre-merge all-zero check), `p102.py`, `p103.py`, `pgdef.py`, `cap.py`. Run: `railway run --service worker C:\Users\Windows\bl-rescat-venv\Scripts\python C:\Users\Windows\bl-checks\<x>.py` from the OneDrive repo dir. (Claude's own `railway run` prod reads sometimes get blocked by the auto-mode classifier — then the owner runs them.) |
| Gate scripts | scratchpad of session `0ade294c`: `gate_iic2.py` (cases A/B/C, env `GATE_EXPLAIN=1`, `GATE_SEED_ONLY=1`), `plan_experiment.py`. Path: `C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/0ade294c-c4e6-4fea-9df4-5d577a1e03b6/scratchpad/`. TEST DB ONLY (they assert it). |

## The goal (Phase 1b-1b = a hard, fair, credit-weighted daily Tracerfy spend cap)

The skip-trace dispatcher SPENDS the operator's money at Tracerfy. Normal lookup = 1 credit,
advanced = 2. Two rolling-24h caps (all tenants / per account). Already LIVE in production:

| Step | PR | State |
|---|---|---|
| 1b-1b-i spend-ledger hardening | #358 | live |
| ii-0 retire out-of-dispatcher spenders | #359 | live |
| ii-a migration 102 (weight CHECK + guard trigger + spent index) | #361 | live |
| ii-b the cap (in-lock READ COMMITTED spend read, fair `row_number`, refill, 12 rounds / 2 s) | #364 | live; prod caps `SKIP_TRACE_DAILY_CREDIT_CAP=2000`, `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP=500` (api+worker); legacy `SKIP_TRACE_DAILY_ROW_CAP=1000` still set, ignored |
| **ii-c-1 migration 103** `ix_pending_skip_trace_queued_frontier (trace_type, user_id, enqueued_at, id) WHERE status='queued'` | **#365 `406cff6e`** | **merged 09-27 06:36Z, VERIFIED LIVE in prod by the object** (valid/ready/live, exact def) |
| **ii-c-2 keyset refill** | — | **WIP, this branch** |

Why ii-c: ii-b's refill re-ranks every eligible queued row every round (window sort) — ~440 ms/round
at 117k queued; owner accepted that envelope ONLY until ii-c lands, which MUST be before Phase 1c
(the frontend action that can create large queues). Cap hardness never depended on this.

## What ii-c-2 changes (done, on this branch)

- `skip_trace_capacity.py`: `FRONTIER_START` (typed sentinel `-infinity` + zero uuid), `Candidate`,
  `discover_accounts()` (recursive-CTE loose index scan over 103 — Codex's query, verbatim; EXPLAIN
  confirms Index Only Scan, 1.4 ms for 50 accounts), `round_limits()` (Codex H2 formula:
  `round_limit = min(5000, global_left*2**r)`; per account
  `min(room_left*2**r, ceil(global_left/active_with_room)*2**r, round_limit)`), new `allocate()` =
  `unnest(4 arrays)` CROSS JOIN LATERAL walk, **ranked OUTSIDE the lateral** (row_number over the
  lateral output), `advance()` (frontier moves over EVERY returned row). `SET LOCAL
  enable_bitmapscan = off` + `RESET` around the walk statement (see failed attempts).
- `skip_trace_dispatcher.py`: `_eligible` → `_candidates(acct)` lateral (eligibility predicates
  UNCHANGED + `user_id = acct.user_id` + `(enqueued_at,id) > (after_at,after_id)` + watermark, ORDER BY
  enqueued_at,id LIMIT acct.lim); loop uses discover → round_limits → allocate → advance; account
  retirement only when the round LIMIT did not bind and the account returned 0 (F3); removed
  `considered` set and `_REFILL_MAX_CONSIDERED`; `_InFlightCache` (read-once-per-account, separate
  `read` set, F5) passed to `_hold_answers_in_flight(..., cache=)` (default None keeps old callers).
- `tests/test_skip_trace_credit_cap.py`: considered_limit test removed; added equal-timestamp tie,
  global-LIMIT-cut-comes-back, round_limits unit tests (4095 cutoff at global_left=1; 15k-account
  bound; zero-room excluded), discovery test; scale test on the 4-array signature; retype and
  full-account tests adapted. **121 pass** (credit cap 102 + 103's 19).

## ~~Current blocker: the S3 performance gate, ROUND 0~~ RESOLVED 2026-09-27

**Resolved:** the watermark inside the lateral made the planner walk the old dispatch index;
moved into `allocate()`, round 0 is 1.2 ms and every gate case passes. Evidence, the Codex
rounds and the mutation proofs: the plan, section "ii-c-2 round 0". The text below is the
state as it was handed over.

Budget: every allocate round ≤ 250 ms, refill ≤ 2 s. Latest `gate_iic2.py` results:

| case | round 0 | rounds ≥ 1 | verdict |
|---|---|---|---|
| A 117k queued / 50 accts / 15k held | **1,172 ms** | not reached (deadline) | FAIL |
| C 35k / 50 / 5k held | **252 ms** | 8–36 ms, all 4,095 held passed | FAIL (round 0 only) |
| B 15k accounts × 2 rows | not run yet | | |

## Failed attempts (do not repeat blindly)

1. ii-c-2 v1 with `row_number()` INSIDE the lateral: round 0 = 1,973 ms (A). The window forced a sort
   of every matching row before LIMIT.
2. Rank moved outside the lateral: still ~1.4 s. EXPLAIN: planner estimates ~16 rows/account (real
   2,340) because frontier/LIMIT are per-account params, and picks a **Bitmap scan on the OLD
   `ix_pending_skip_trace_dispatch`** reading all 117k queued rows per account (loops=50).
3. `plan_experiment.py` (one statement, 50 accounts, lim 1): V1 joins-in-lateral 607 ms; V2 EXISTS
   470 ms; V3 `OFFSET 0` fence 249 ms (uses 103 but reads all rows); **V4 V1 + `SET LOCAL
   enable_bitmapscan=off` 2.8 ms** (ordered index walk that stops at LIMIT). → implemented in
   `allocate()`.
4. With V4 in place, rounds ≥ 1 are fast but **round 0 inside the real pass is still slow**. Round 0's
   plan has NOT been captured: `gate_iic2.py`'s `_Explain` proxy EXPLAINs the FIRST `execute`, which
   is now the `SET LOCAL` text statement. Fix the proxy to skip non-`Select` statements (check
   `hasattr(stmt, "selected_columns")`), then `GATE_EXPLAIN=1 ... gate_iic2.py A`.
   Hypotheses to test: (a) in round 0 all 50 accounts have lim=1 and a hash/merge plan or seq scan
   wins (bitmap is off, seq scan is not); (b) jobs seq scan per account; (c) the `_duplicated_result_ids`
   / `_job_delivered_sql` text predicate changes the plan vs the experiment's simplified predicates.
   Try also `enable_seqscan=off` scoped the same way, or `SET LOCAL enable_hashjoin/mergejoin=off`,
   and compare with the experiment's exact 4-variant method before choosing. Record evidence in the
   code comment like the existing one.

Other gotchas this session: CRLF files (`tasks/*.md`, ruff-rewritten py) make LF-anchored node/python
`replace` silently no-op → use the Edit tool or normalize `\r\n`; bash heredocs mangle `\n`/backticks →
write scripts with the Write tool; background tasks get REAPED for low memory while idle → run Codex
and long pytest in the FOREGROUND (`timeout 590`); another session merges to `main` often → fetch +
rebase right before pushing (Codex compares with current origin/main and reads missing commits as
reversions).

## Next steps (in order)

1. Capture round 0's plan (fix the proxy), fix it, get gate A/B/C green (≤ 250 ms every round,
   refill ≤ 2 s). Record the evidence (numbers + chosen planner setting) in the `allocate()` comment
   and the plan's ii-c section.
2. Mutations (break each, a test must fail): frontier advanced only over survivors; retire on short
   result; cache without the `read` set; round_limits last term = `global_left`; rank inside lateral
   (perf, not a test).
3. Full skip-trace regression in batches (memory is tight; see the batch lists used for ii-b:
   dispatcher_claim, spend_ledger, dispatcher_credits, over_quota, settled_complaints,
   already_delivered, reconciliation, lookup_subject_reuse; then claim, eligibility,
   enqueue_after_delivery, foreign_address, tenant_gates, tracerfy_ingest, cache_key, url_secrecy,
   lookup_subject_key, pending_skip_trace_weight, pending_skip_trace_frontier_index,
   contact_lookup_schema).
4. Squash/reword the WIP commit, Codex diff review in the FOREGROUND until GO (prompt pattern: see
   `codex_iic1_review*.txt` in the scratchpad), fetch+rebase, push, PR, CI (foreground watch,
   two 10-min windows), `quiet.py` all zeros, merge pinned to head (`--match-head-commit`), watch the
   first dispatcher tick in worker logs.
5. **Safety PR** (plan Deferred bullet 1): pin/clear `DATABASE_URL_MIGRATE` in
   `tests/_db_safety.py`; stop env.py loading `.env` implicitly for non-boot invocations.
6. Then 1b-1b-iii (pause state: resume-time query, Redis publish w/ heartbeat, beat interval setting,
   scheduler.py). Carry H3 into 1b-2 (action worker uses `lock_job_for_claim()` +
   `claim_skip_trace_rows()` only; two-session test).

## Working agreements (owner)

- Codex in the loop on EVERY step: pre-code consult until PLAN: GO, diff review until VERDICT: GO.
  Where Codex and Claude disagree and docs are silent, Codex wins — unless its fix breaks money
  safety (e.g. REPEATABLE READ was rejected with proof; Codex then confirmed).
- Merge IS deploy (Railway migrates on boot): all-zero `quiet.py` BEFORE merging; verify by objects.
- 5-file rule per PR; no mocks except the external Tracerfy boundary (tests use the http:// base-URL
  trick: a definite rejection releases every claimed row to 'errored').
- Never push/merge without the owner's go for a merge; commits/branches in the worktree are fine.
