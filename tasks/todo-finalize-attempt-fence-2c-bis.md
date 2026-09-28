# UX queue 2c-bis: fence finalization to the attempt that owns the job

Branch `feat/finalize-attempt-fence-2c-bis` (worktree `C:/Users/Windows/bl-wt/fence`), from
`origin/main` `c0b09b7a`. No migration. A HARD PREREQUISITE of 2c
(`tasks/todo-run-count-breakdown-2c.md` on `feat/run-count-breakdown-2c`): merged, deployed
and verified before 2c merges. Owner approved the order 2026-09-28.
Status: **PLAN r3. Waiting on Codex PLAN: GO.** (r1, r2: CHANGES, folded in below.)

## The defect (pre-existing; found by Codex in the 2c plan review, r1 P1-1)
The watchdog re-queues a stuck job (`scheduler_helpers/health.py:300-330`: `status='pending'`,
`started_at=NULL`, `retry_count+1`); so does a transient retry (`status.py`
`_retry_scrape_job`, same three writes). A replacement attempt B then claims it
(`claim_job_for_attempt`: `status='queued'`, new `started_at`). If the old attempt A was
STALLED, not dead, and resumes, nothing in its finalization knows about attempts:
- billing CAS `tasks.py:2250-2254`: `WHERE id AND billing_applied_at IS NULL`. A bills its
  own view and charges the user.
- done-CAS `tasks.py:2407-2413`: no `expected_started_at`; `_set_status` refuses only
  TERMINAL rows and B's row is queued/scraping/enriching, so A marks B's run done.
- `_set_stage(... "finalizing", expected_started_at=...)` is scoped but its result ignored
  (`tasks.py:2204`).
- billing-failed branch `tasks.py:2367-2383`: `_fail_job(db, job, r, job_id, reason)` with
  no `expected_started_at` (Codex r2 P1-2): stale A could mark B failed.
B later finds the job billed ("already billed" path) or terminal. Today the releases happen
to no-op (A billed; the claim release needs `status='cancelled'`), so the damage is: billed
and "done" from A's stale view, B's work discarded.

The trap: once the done-CAS is fenced, stale A's done-CAS FAILS while B runs, and the
existing failure branch calls `release_quota_reservation`, which is guarded by
`billing_applied_at IS NULL` and the window, NOT by status or attempt: it would refund the
grant B reuses (the plan cap's `reserved_at` CAS lets a re-run reuse the first grant). So
"lost ownership" must be its own exit, never the terminalized one.

## The attempt token (Codex r2 P2-5)
`AttemptToken(started_at, retry_count)`, both from the claim's own `UPDATE ... RETURNING`.
Deterministically unique per claim, no clock argument: a claim needs `status='pending'`;
the only two writes that put a CLAIMED job back to pending (watchdog re-queue,
`_retry_scrape_job`) both do `retry_count = retry_count + 1`; so no two successful claims of
one job share a `retry_count`. (Verified by grep: every other `status="pending"` write in
`src/` INSERTS a new row.) A test pins the two re-pend writes' increments.
New `claim_attempt(db, job_id) -> AttemptToken | None` does today's claim UPDATE plus
`RETURNING retry_count`; `claim_job_for_attempt` stays as a thin wrapper returning
`token.started_at` for legacy callers and tests only. run_scrape_job uses `claim_attempt`
and holds ONE variable, `attempt_token: AttemptToken` (Codex r5 P1-2), which it passes to
EVERY attempt-scoped call; no `token.started_at` is ever passed on its own anywhere in
run_scrape_job or finalize.py (B7 asserts it).

Scope of the composite token (Codex r3 P1-2 and r4 P1-1/P1-2/P2-3: EVERY attempt-scoped
write, not just finalization). All of them live in two files: `tasks.py` passes
`attempt_started_at` to the `status.py` helpers at 19 sites, and `status.py` builds the
predicates. So run_scrape_job's `attempt_started_at` is replaced by `attempt_token`, and each
`status.py` helper that takes `expected_started_at` (`_set_status`, `_set_progress`,
`_set_stage`, `_fail_job`, `_retry_scrape_job`, `_write_heartbeat` / `HeartbeatThread`)
accepts either a bare datetime (every existing caller and test, behavior unchanged) or an
`AttemptToken`, via ONE `_attempt_clauses(token)` (ORM) / `_attempt_sql(token)` (raw SQL)
that adds `retry_count = token.retry_count` beside `started_at = token.started_at`. The 19
call sites change only their argument (`expected_started_at=attempt_token`). `retry_count` is constant for an attempt's whole life: the
only writes that change it are the two re-pends that END an attempt. Test B8 forces a
collision (B's claim stamped with A's exact `started_at`, higher `retry_count`) and drives
every helper with A's token: none writes.

## What this change guarantees, exactly (Codex r6 P1-1, P2-3)
Two cases, kept apart (Codex r7 P1-1):
- LOST OWNERSHIP = the token changed and the row is still live (another attempt holds, or
  is about to hold, the job). INVARIANT: such an attempt performs NONE of the finalization
  effects: no billing CAS, no users settle, no done or failed transition, no
  quota-reservation release, no claim release, no completion/failure notification, job-log
  write, log publish, email, webhook or dialer push. After ownership is lost, none of the
  status.py attempt-scoped writes (stage, progress, heartbeat, retry) lands either.
- TERMINALIZED = the row is terminal (cancelled/failed/done), whatever the token. This is
  today's cleanup, unchanged and deliberately allowed: `release_quota_reservation` and
  `_release_claims_of_cancelled_job` re-check their own guards (`billing_applied_at IS
  NULL` + window; `status='cancelled'` + unbilled), so whoever finds the job terminal can
  run them and they fire at most once. A cancelled job's resources therefore always have
  an owner of their cleanup.
Guarantee scope (Codex r7 P2-5): it holds for production callers, which all pass an
`AttemptToken` (B7 asserts it). The bare-datetime form stays only for legacy test callers
and keeps today's timestamp-only behavior; no production path uses it.
NOT covered, stated as pre-existing and not money-moving, each with why:
- plan cap (`tasks.py:1734-1926`): the reservation is a once-per-JOB CAS (`reserved_at IS
  NULL`); a second attempt reuses the grant and never charges again. The over-quota marks
  are cleared and recomputed by every pass from the same grant and the same deterministic
  ranking, so a stale pass rewrites the same marks. If it ran after B finished, it changes
  the LIVE view only; B's bill and (after 2c) its frozen breakdown are unaffected.
- enrichment / owner flags / NTS match / survivor re-election on `results`: rows are shared
  by attempts by design (idempotent insert on `(job_id, source_fingerprint)`); a stale write
  after B finished is the same class as a post-completion backfill (live view only).
- the R2 export OBJECT CONTENTS (Codex r7 P2-3): the key is job-scoped
  (`exports/{user}/{job}/leads.<ext>`, `tasks.py:1487`), so a stale A upload CAN overwrite
  it. Excluded explicitly because no customer receives that object: the download
  (`jobs.py` `download_export`) rebuilds the CSV from `results` at request time and only
  uses `export_key` as a gate, and the R2 presign path is broken in prod (401, BACKLOG §4).
  Queued with the follow-up (attempt-unique key) rather than fixed here.
Widening the fence to those writes is a separate change (queued as a follow-up), not 2c-bis.
B4's snapshot is scoped to the finalization effects above plus the status.py writes.

## Fix
F1. ONE live-state helper (Codex r2 P1-1), `status.py`:
    `attempt_state(db, job_id, user_id, token) -> AttemptState(owned: bool, status: str)`:
    `SELECT started_at, retry_count, status FROM jobs WHERE id AND user_id FOR UPDATE`;
    `owned = (started_at, retry_count) == token AND status NOT IN _TERMINAL_STATUSES`
    (Codex r3 P1-1: a cancel keeps the token, so a token match alone is not ownership;
    terminal takes precedence). Always called on a clean transaction (after a rollback),
    and the caller ends that transaction on every exit.
F2. ONE exit decision, `finalize_exit(state) -> "lost_ownership" | "terminalized" | None`:
    `status in _TERMINAL_STATUSES` (imported from status.py) -> "terminalized", whatever
    the token; else owned -> None (carry on); else "lost_ownership". Acceptance check
    (Codex r3 P3, narrowed to finalization): finalize.py and F1/F2 name no terminal status
    literal; the hard-coded list in `_retry_scrape_job` is outside this change.
F3. The billing + done-CAS sequence MOVES VERBATIM into `src/workers/tasks_helpers/finalize.py`
    `finalize_billing_and_done(db, r, *, job, user, config, job_id, token, object_key,
    boot_user_id) -> FinalizeOutcome` (Codex r1/r2 P2: the tests must run production code).
    Side effects INSIDE the helper (so every outcome is testable): the billing reads and CAS,
    the users settle, the done-CAS and its commit, the terminalized branch's
    `release_quota_reservation` + `_release_claims_of_cancelled_job`, the billing-failed
    branch's `_fail_job` + `job_failed` notification (both exactly as today). OUTSIDE (in
    run_scrape_job, unchanged, only on `DONE`): the overage/completion logs, the done
    publish, and the rest below. Claim cleanup (Codex r3 P2-5):
    `_release_claims_of_cancelled_job` and its alert helper MOVE from tasks.py into
    finalize.py (tasks.py imports them back for its other caller); they keep their own
    commit/rollback exactly as today, and run only on the TERMINALIZED outcome, after the
    finalization transaction was rolled back. Also OUTSIDE, on `DONE` only: the completion
    notification, email, webhook, dialer. Outcomes: `DONE(display_count)`,
    `ALREADY_TERMINAL` (the existing force-finalize / terminalized path), `LOST_OWNERSHIP`,
    `BILLING_FAILED`. Commit 1 = the pure move (no statement changes, suite green); commit 2
    = the fence below, reviewable alone.
Lock placement (Codex r4 P2-4): `_set_stage("finalizing")` commits on its own, before the
billing transaction opens (as today). The billing transaction then starts with
`attempt_state(... FOR UPDATE)`: owned -> the jobs row stays locked through the billing
reads, the billing CAS, the users settle and the done-CAS, which commit together (no commit
in between, as today); a cancel issued in that window waits and then finds `done`. Not
owned -> F2's exit before any billing read.
F4. Fence, each point using F1+F2, each lost-ownership exit = `db.rollback()`, one INFO log
    "attempt token changed or job terminal; not finalizing", return `LOST_OWNERSHIP`: nothing billed, released,
    published or notified.
    - stage write `_set_stage(... "finalizing", ...)` returns False (a CAS miss OR a
      swallowed telemetry error: `_set_progress` never raises) -> F1/F2 (Codex r6 P2-2):
      terminalized -> today's force-finalize handling; lost -> exit; OWNED -> it was a
      telemetry failure: proceed exactly as today (the billing transaction's own
      `attempt_state ... FOR UPDATE` is the gate that matters).
    - billing CAS WHERE adds `_attempt_clauses(attempt_token)` (both predicates). rowcount 0 -> F1/F2: owned -> today's already-billed path;
      terminalized -> today's terminalized branch; lost -> exit.
    - done-CAS: `_set_status(..., expected_started_at=attempt_token, commit=False)`
      (both predicates via `_attempt_clauses`). False -> rollback -> F1/F2: terminalized -> today's branch
      (releases re-check their own guards); lost -> exit.
    - billing-failed branch (Codex r7 P1-2): after its rollback, F1/F2 FIRST: terminal ->
      today's terminalized cleanup (as above); lost -> exit; owned ->
      `_fail_job(..., expected_started_at=attempt_token)`, which releases the reservation
      itself as today. The remainder of this bullet is the owned case. (Codex
      r2 P1-2, r5 P1-1: the full token, so a same-timestamp collision cannot fail B, release
      its reservation or notify); its CAS losing means ownership was lost -> no
      notification. B8 drives this branch end to end under a forced collision.

## Tests (real DB, each proven RED on unfixed code; all through production helpers)
- B1 `attempt_state` / `finalize_exit`: owned; re-queued (NULL); re-claimed (new token);
  every value of `_TERMINAL_STATUSES` -> terminalized; `FOR UPDATE` actually locks (a
  second session's UPDATE waits).
- B2 token uniqueness: re-queue + claim twice -> distinct `retry_count`; the watchdog and
  `_retry_scrape_job` re-pend writes both increment (drives the real functions).
- B3 move is behavior-neutral: `finalize_billing_and_done` for an owned attempt -> `DONE`,
  billed once, users counter delta = billable - reserved, row done; already-billed re-run
  -> reports `billed_count`, no second charge; cancelled row -> `ALREADY_TERMINAL`, reservation
  released once, claims released.
- B4 races, stale A vs replacement B, two sessions, named (Codex r2 P2-4):
  - `test_b_reclaims_before_a_stage_write`
  - `test_b_reclaims_between_stage_and_billing`
  - `test_a_holds_billing_lock_so_a_wins_and_b_requeue_noops`
  - `test_b_reclaims_before_a_done_cas_on_the_already_billed_path` (Codex r3 P1-3: after a
    FRESH bill A holds the row lock through the done-CAS, so B cannot get in between; the
    only way to reach A's done-CAS unlocked is the already-billed re-run, whose billing CAS
    does not fire. Precondition asserted: `billing_applied_at` set before A starts.)
  - `test_a_terminal_row_with_the_same_token_is_terminalized` for every
    `_TERMINAL_STATUSES` value (Codex r3 P1-1).
  Assertions (Codex r3 P2-4, r7 P2-4): a SNAPSHOT of the job row, the user row, the
  reservation, the claims, the notifications, the `job_logs` rows and the Redis channel is taken at the barrier just before
  A's step; after A returns `LOST_OWNERSHIP` every one is unchanged: A wrote NOTHING AFTER
  IT LOST OWNERSHIP (Codex r5 P2-3). Writes A made while it still owned the job (its
  committed stage/progress telemetry before B re-claimed) are legitimate and allowed; the
  barrier is placed at the ownership change, so they are outside the snapshot window.
  B's settlement is asserted separately in B5. The A-wins case asserts the opposite:
  A `DONE`, billed once, `status='done'` with A's token, B's re-queue rowcount 0, claim
  None.
- B5 the replacement cleans up exactly once, after A lost (Codex r1 P1-2):
  - `test_after_a_lost_b_succeeds_billed_once`
  - `test_after_a_lost_b_is_cancelled_releases_once`
  - `test_after_a_lost_b_fails_releases_once` (`_fail_job` path)
- B6 billing-failed branch: lost token -> no `_fail_job`, no notification, nothing
  released; row cancelled in between -> terminalized cleanup releases the reservation and
  claims once; owned -> `_fail_job` as today (Codex r7 P1-2).
- B7 wiring (supplement only): run_scrape_job calls `claim_attempt` and
  `finalize_billing_and_done` and maps outcomes; email, webhook and dialer are reached ONLY
  on `DONE` (Codex r7 P2-4: they are outside the helper, so this is where they are pinned); every attempt-scoped call in
  run_scrape_job and finalize.py passes `attempt_token`, never `.started_at` alone; mutation
  checks on every fence point. Logs and docstrings say "attempt token changed", not
  "started_at moved" (Codex r5 P3).
- B8 forced timestamp collision (Codex r4): B's claim stamped with A's exact `started_at`
  but `retry_count + 1`; with A's token, `_set_status`, `_set_progress`, `_set_stage`,
  `_fail_job`, `_retry_scrape_job`, `_write_heartbeat` and the billing CAS each write
  nothing (rowcount 0 / False), and `attempt_state` reports not owned. With a bare
  datetime (the legacy call form) each still behaves exactly as today (regression).
  So after ownership is lost, stage/progress/heartbeat writes are fenced too, and the B4
  "nothing after the loss" snapshot covers them (Codex r4 P2-3).
Then the full suite (8 parts), ruff, security review x2, Codex diff review to GATE: PASS.

## Deploy
No migration; a money-path worker change: quiesce (quiet.py zeros, no non-terminal jobs),
merge `--match-head-commit`, verify SHA on api/worker/beat, health, logs. Then 2c rebases
onto it (its snapshot decision moves into `finalize_billing_and_done`; its T8 switches to the
production helpers).

## Steps
- [ ] 1. Codex PLAN review until PLAN: GO.
- [ ] 2. Commit 1: F3 pure move + B3 (green before and after the move).
- [ ] 3. `claim_attempt`, F1, F2 + B1, B2.
- [ ] 4. Commit 2: F4 fence + B4-B7 (RED first) + mutations.
- [ ] 5. Full suite, ruff, security review x2.
- [ ] 6. Codex diff review `origin/main...HEAD` until GATE: PASS.
- [ ] 7. Quiesce, merge, deploy, verify.

## Review
(filled in after the build)
