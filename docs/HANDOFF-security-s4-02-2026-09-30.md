# Handoff: S4-02 + PyJWT 2.14.0 shipped (2026-09-30), what is left

**Read this first. Then read the top entry of `docs/BUILD_JOURNAL.md` (2026-09-30, "S4-02
fixed ... and a merge that silently broke the fix").** The previous handoff is
`docs/HANDOFF-security-s4-06-2026-09-28.md`. Its §4 (failed attempts), §6 (test env) and
§7 (merge procedure) still apply, with the corrections in §5 below.

## 1. Current state (verified 2026-09-30)

Merged and live this session (api, worker and beat all SUCCESS on each commit):

| PR | What | Merge commit |
|---|---|---|
| #388 | S4-06 handoff doc | `6675a60a` |
| #391 | PyJWT 2.13.0 -> 2.14.0 (10 CVEs; the required Dependency Audit had blocked every PR) | `0539de2b` |
| #389 | **S4-02**: a cancelled run holds its scraper's slot until its worker stops | `02664e60` |

Also merged by another session in between: #390 (UX 2c-bis attempt fence, `fd200257`).
That session also has #386 (contact lookup) in flight.

Audit #4 code findings are **all fixed and live**: S3-03/S4-01, S4-03, S4-06, S4-02.

### How S4-02 works now (for anyone touching run slots or the heartbeat)

- `HeartbeatThread.__exit__` runs after the work session closes, on every exit of
  `run_scrape_job`. It calls `_acknowledge_exit()` in `src/workers/tasks_helpers/status.py`:
  `last_heartbeat_at = NULL` on its OWN attempt (`_attempt_sql(token)`), and only when the job
  is `cancelled`. Errors are logged and swallowed; Celery time limits are re-raised.
- `Job.holds_run_slot()` (no argument now): a cancelled row holds the slot while
  `started_at IS NOT NULL AND last_heartbeat_at IS NOT NULL AND started_at > now() -
  RUN_SLOT_RELEASE_AFTER_S` (the 3900 s hard limit + 120 s), all by the DB clock.
- `claim_attempt` stamps `started_at` and `last_heartbeat_at` with the DB's `now()`.
- The heartbeat starts right after the claim, before the gates that can return.
- **Invariant:** NULL `last_heartbeat_at` on a started, cancelled row means "the worker
  acknowledged its exit". Any NEW code that NULLs `last_heartbeat_at` must also NULL
  `started_at`, or it will release a live run's slot.
- **Behaviour:** a clean cancel frees the scraper at once. A worker killed mid-cancel
  (deploy or OOM) blocks its scraper until ~67 min after that run started.

## 2. What is left (priority order)

1. **Journal PR #392** (this doc, the journal entry, the S4-02 plan outcome, and a §14 Low:
   the `heartbeat_sync_session` docstring). Hold merges while #386 lands; the other session
   asked for that.
2. **4b-ii (audit #4):** the held-lead customer log line in `enrich.py`
   (`tasks/todo-security-audit4b.md`). #390 rewrote finalization into
   `src/workers/tasks_helpers/finalize.py`, so re-read the design against it first.
3. **5b-ii (P3):** move these onto `pinned_session()`:
   - `src/scrapers/enrichment/pacs.py:137`
   - `src/scrapers/templates/acclaimweb.py:1042,1067`
   - `src/scrapers/enrichment/skip_trace.py:630,702`
4. **D5-03 rollout (owner/ops):** `SCRAPER_EGRESS_PROXY_ENABLED` is OFF. Run every county
   template through the proxy (staging or a quiet window) before turning it on.
5. **`.env.example` (owner; Claude is blocked from reading it):** add
   `SCRAPER_EGRESS_PROXY_ENABLED=false` and `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE=25`.
6. **D5-01 (P3, product decision):** apply `AI_JOB_LIMITS` in scheduler/batch dispatch?
7. **S3-16 (P2, external):** Tracerfy must send `X-Tracerfy-Webhook-Secret`; then remove the
   legacy path-secret route.
8. **S4-07 (P3):** owner's choice (lower JSON `page_size` cap, or leave).
9. **Owner infra:** Cloudflare as sole ingress (S3-04), admin password rotation (S3-17), GH
   `production` environment protection, Tracerfy spend caps.
10. **Unexplained:** the Supabase DB stall of 2026-09-29 ~01:28-01:45Z (owner dashboard).

## 3. Failed attempts and dead ends (do not repeat)

- **A merge silently broke the fix.** #390 retyped the attempt token; the conflict resolution
  bound it as a bare `started_at` inside a swallowing `except`. Only the test that drives the
  REAL `run_scrape_job` caught it. After any merge into a long-lived branch, grep every new
  consumer of a retyped value and prove a real-path test fails without the fix (memory
  `clean_rebase_can_still_drift_behavior`).
- **Main can move between the quiet check and the merge.** #389's merge was refused for
  conflicts because #390 landed in that window. Re-fetch right before `gh pr merge`, and
  always pin `--match-head-commit`.
- **Codex re-raises settled points after a merge.** The "legacy NULL-heartbeat rows" P1 came
  back in r3 after r2 had accepted the evidence. Answer with the evidence again, don't
  redesign.
- **`quiet.py` can show a transient long transaction** (1 session > 30 s). Re-poll it in a loop
  (30 s apart); never merge on a non-zero count.
- **`railway run` fails in a new worktree** ("No linked project"). Run `quiet.py` from the
  main checkout.
- The auto-mode permission classifier blocks `gh pr merge` (production deploy) unless the
  owner has said to merge in this session.

## 4. Where things are on disk

| Path | Branch | State |
|---|---|---|
| `C:/Users/Windows/bl-wt-journal-s402` | `docs/journal-2026-09-30-s4-02` | #392, open; remove the folder after it merges |

Removed this session with the owner's OK (folders only; branches kept): `bl-wt-s402` (#389),
`bl-wt-pyjwt` (#391), `bl-wt-s406` (#388), the temp venv `bl-venv-pyjwt214`, and the local
DB `bridgeleads_s402_test`.

Never delete or force-move branches. Removing worktree folders needs the owner's OK.

## 5. Test environment (corrections to the S4-06 handoff §6)

- This session used its own DB `bridgeleads_s402_test` (now dropped; recreate it from the
  secaudit5 `testenv.sh` with the DB name and Redis index changed, then run
  `alembic upgrade head`). Redis **13** was free. Redis 11 and 12 hold another rig's Celery
  keys; don't use them.
- The shared venv `bl-rescat-venv` is still on **PyJWT 2.13.0**. Production is on 2.14.0.
  Run `pip install "PyJWT[crypto]==2.14.0"` there when no other session is mid-run.
- Suites for this area: `test_run_slot_cancel_s4_02`, `test_run_in_flight_guard`,
  `test_config_eligibility`, `test_finalize`, `test_finalize_fence`, `test_workers`,
  `test_batch_*`, `test_dispatch_due_jobs`, `test_scheduled_job_dedup`.
