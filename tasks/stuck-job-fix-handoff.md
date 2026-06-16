# Handoff — Stuck "running" scrape job on admin account

> ⏩ **EXECUTION PROGRESS (updated 2026-06-16) — read this first, then the original brief below.**
> **NEXT ACTION for the new session: resolve the ONE open Codex [P1] in `create_job` (wrap `apply_async` in
> try/except → return the committed JobResponse instead of 500-ing; see "PHASE 2 — IMPLEMENTED" below), then
> run the security Master Review, then ruff/pytest, then STOP for user approval before Phase 3.**

## ✅ PHASE 0 — CONFIRM ON PROD — DONE (2026-06-16)
Root cause confirmed EXACTLY. Read-only diagnostics found it's bigger than one job:
- **105 active jobs, ALL `pending` / `retry_count=0` / `started_at=NULL`**, across **58 users**,
  oldest 37 days. `started_at=NULL` everywhere ⇒ worker atomic claim never succeeded.
- Intermittent (~6% per-job loss), NOT a total outage: 24h = 53.6% done / 7.1% stuck; 72h = 61.5% / 5.8%.
- Worker + beat HEALTHY: good jobs claim in ~1s (started_at≈created_at+1s = the race window); beat fires
  `watchdog_stuck_jobs` every 5 min (returns None — finds nothing, because it skips rc=0 pending).
- Tools (read-only, kept): `scripts/diag_stuck_jobs.py`, `scripts/diag_job_status_rates.py`.

## ✅ PHASE 1 — UNSTICK — DONE (2026-06-16). User chose **FAIL-CLEAN ALL 105**.
- `scripts/failclean_orphaned_pending_jobs.py` (dry-run default; `--commit` applies). Guarded raw UPDATE
  with CAS `status='pending' AND started_at IS NULL AND retry_count=0` (can't clobber a just-claimed job;
  no ORM/Celery side-effects). Result: **105 → `failed`** w/ clean non-leaking message; **0 remaining**.

## 🟡 PHASE 2 — DURABLE BACKEND FIX — IMPLEMENTED (2026-06-16), 1 open Codex [P1] before NO-GO clears.
**Codex consult DONE (Opt A vs B): chose Option A + watchdog. "Do NOT do B in the same fix."** Both edits made:
- `src/api/routes/jobs.py` `create_job`: added `await db.commit()` after `db.flush()` and BEFORE
  `run_scrape_job.apply_async()` (commit-then-enqueue). Teardown double-commit is benign (9-route precedent).
- `src/workers/scheduler_helpers/health.py` watchdog: added 3rd OR branch + loop handler for orphaned
  fresh pending (`status='pending' AND retry_count==0 AND started_at IS NULL AND created_at < now-10min`) →
  re-delivers via `.delay()` (atomic CAS dedupes), NO retry_count mutation, distinct log line.
- ✅ ruff clean; ✅ 6/6 watchdog tests pass (`pytest tests/test_workers.py -k "watchdog or stuck or pending or stranded"`).

**Codex review of the diff — findings triaged:**
- ❌ [P1] expire_on_commit lazy-refresh = **FALSE POSITIVE** — `AsyncSessionLocal` IS `expire_on_commit=False`
  (session.py:65); `created_at` server_default is eager-fetched on flush. No `db.refresh()` needed. RESOLVED.
- ⚠️ **[P1] OPEN — publish-failure semantics:** if `apply_async` raises, route now 500s with a committed
  `pending` job → client retry → duplicate. **FIX (do this first in new session):** wrap `apply_async` in
  try/except in create_job; on failure `_logger.warning(...)` and STILL `return JobResponse.model_validate(job)`
  (job is committed pending; watchdog delivers ≤10min). This is the NO-GO blocker.
- ✅ [P2] RLS scoping — satisfied: watchdog uses `system_sync_session()` (cross-tenant system role by design).
- 🔵 [P2] optional follow-ups (defer): redelivery re-sends every 5-min tick until claimed (add `last_enqueued_at`
  throttle if it ever matters); watchdog log wording slightly stronger than state proves (cosmetic).

**Remaining Phase 2 steps:** (1) fix the open [P1]; (2) `codex review` again to confirm clean + security
Master Review (`.claude/rules/security.md`); (3) ruff + pytest; (4) STOP for user approval before Phase 3.
**Nothing committed yet** — all edits are in the working tree on branch `test/ui-tax-date-column`.

<!-- superseded analysis kept below for reference -->
## (superseded) PHASE 2 — original analysis
Full `get_db` consumer audit completed (READ-ONLY). Key reframing:
- `get_db` (`src/db/session.py:69-77`) ALREADY commits-on-success-after-yield + rollback-on-exception.
- **9+ type-B routes ALREADY `await db.commit()` AND then get the teardown commit** — a benign
  double-commit is ALREADY in prod (2nd commit on clean session = no-op). Precedent:
  `update_notification_preferences`(auth.py:197), `logout_all`(278), `login_mfa_redeem`(login.py:193),
  `login_break_glass_redeem`(352), `change_user_password`(password.py:96), `reset_user_password`(258),
  `mfa_setup_secret/enable/disable`(mfa.py:45/118/196), `create_batch`(batches.py:185),
  `replay_dialer_push`(scrapers.py:588).

**Option A (RECOMMENDED, blast radius = 1 route + watchdog):** in `create_job` (`jobs.py:153-170`),
add `await db.commit()` after `db.add(job); await db.flush()` and BEFORE `run_scrape_job.apply_async()`.
Leave `get_db` alone. RLS-safe: `after_begin` listener `_reapply_rls_guc` (session.py:165-170) re-binds
`app.current_user_id` from `session.info['rls_user_id']` on EVERY tx (survives the commit); `audit_log`
is fire-and-forget on its own bg session (security.py:570); `JobResponse.model_validate(job)` needs no DB.
Proven safe by the 9 precedent routes.

**Option B (heavier, NOT preferred):** stop teardown auto-commit + add explicit commits to all 9 type-C
routes (register_user, create_scraper, delete_scraper, create_connector, get_cached_records[GET that
mutates user_record_views!], create_job, cancel_job, create_checkout, stripe_webhook×4).

**Watchdog fix (`src/workers/scheduler_helpers/health.py`):** add OR branch
`and_(status==PENDING, retry_count==0, started_at IS NULL, created_at < now-10min)`; handle like the
existing "stranded retry" branch — re-deliver via `run_scrape_job.delay(jid)` (atomic CAS dedupes),
**no retry_count mutation**, distinct log line. Race-free (row already committed). Do NOT add PENDING to
`STUCK_CHECK_STATUSES` (constants.py:69 — excluded by design); add a dedicated branch.

**Phase 2 order:** (1) Codex consult A vs B → reconcile; (2) implement in jobs.py + health.py;
(3) `codex review`/`codex challenge` + security Master Review (Critical/High = NO-GO); (4) ruff/pytest;
STOP for user approval.

## ⏳ PHASE 3 — UI HONESTY (bridgeleads-web) — NOT STARTED
Map `pending` → "Queued"/"Waiting" not "Running" (`lib/utils.ts` isRunning + RUNNING_STATUSES); add
dashboard auto-refresh/polling for active statuses; tsc+eslint clean; Codex review.

**Uncommitted artifacts (nothing committed yet):** the 3 scripts above + this progress block.

---

> Paste the fenced block below into a fresh Claude Code session to continue.
> Diagnosis is code-confirmed (LLM Council + Codex consult, prior session). Do
> NOT re-run the council. Pick up at execution, one phase at a time.

```
CONTEXT HANDOFF — Stuck "running" scrape job on admin account (BridgeLeads)

We already ran the LLM Council + a Codex consult last session. Root cause is
CODE-CONFIRMED. Do NOT re-run the council. Pick up at execution. Follow the
project rules in CLAUDE.md + .claude/rules/ (security baseline, no mock/dummy
code, every query filters by user_id, errors never leak stack traces).

== CONFIRMED ROOT CAUSE ==
Single-job create path has an enqueue-before-commit race:
- src/api/routes/jobs.py create_job() calls run_scrape_job.apply_async() INSIDE
  the request transaction.
- src/db/session.py get_db (and get_rls_db wrapping it) only commit AFTER the
  route returns (yield session; await session.commit()).
- If a worker consumes the message before that commit lands, the atomic claim at
  src/workers/tasks.py:151 (UPDATE jobs SET status='queued',started_at=now()
  WHERE id=:id AND status='pending') gets rowcount=0 and bails. Row commits stuck
  in 'pending'.
- Watchdog (src/workers/scheduler_helpers/health.py) DELIBERATELY excludes fresh
  pending (retry_count=0), so the orphan is never recovered.
- Frontend (bridgeleads-web lib/utils.ts isRunning + RUNNING_STATUSES) renders
  pending as a spinning "Running" with no dashboard auto-refresh.
The BATCH path already fixed this (commit-then-enqueue + lease/CAS on batch_runs);
the single-job path was just never migrated.

== RECONCILED PLAN (Council + Codex agreed: minimal-correct, NOT a full outbox) ==
Codex's critical caveat: get_db commits in teardown, so adding an explicit
route-level commit causes a DOUBLE commit. The fix MUST also make get_db
rollback-on-exception-only (routes own explicit commits) OR add a dedicated
explicit-transaction session dependency. Missing this turns the fix into a new bug.

== EXECUTE ONE PHASE AT A TIME. STOP for my approval between phases. ==
Each phase: orchestrate agents where it parallelizes, then Codex verifies before
I approve moving on. Any Critical/High from Codex = NO-GO until fixed.

PHASE 0 — CONFIRM ON PROD (read-only, do this FIRST, no code changes):
  - railway run the query:
    SELECT id, status, retry_count, started_at, created_at, now()-created_at AS age
    FROM jobs WHERE status IN ('pending','queued','probing','scraping','enriching')
    ORDER BY created_at DESC;
  - Check worker + beat liveness in railway logs (rule out Beat/worker down or a
    genuinely long-running scrape vs an orphaned pending).
  - Report findings. Decide which case is actually true before any fix.

PHASE 1 — UNSTICK THE ONE JOB (idempotent):
  - If orphaned pending: re-mint a token + POST prod /jobs path OR re-enqueue per
    project convention (run_scrape_job is NOT runnable via railway run —
    redis.railway.internal unreachable). If genuinely dead: fail it cleanly.
  - Prove the dashboard no longer shows it running.

PHASE 2 — DURABLE BACKEND FIX (commit-then-enqueue):
  - Move apply_async to AFTER commit in jobs.py.
  - Fix get_db so it does NOT double-commit (rollback-on-exception-only, or a
    dedicated explicit-tx dependency).
  - Add age-gated watchdog branch: pending AND retry_count=0 AND started_at IS NULL
    AND created_at < now()-interval '10 min' (safe via existing atomic claim).
  - Log watchdog requeues + claim-misses separately as normal idempotency outcomes.
  - Codex review the diff (codex review / codex challenge). Run security Master
    Review (.claude/rules/security.md).

PHASE 3 — UI HONESTY (bridgeleads-web):
  - Map pending -> "Queued"/"Waiting", not "Running".
  - Add dashboard auto-refresh/polling for active statuses.
  - tsc + eslint clean. Codex review the diff.

DEFERRED (later hardening, NOT now unless prod evidence shows it): transactional
outbox, terminal-write CAS state-transition helper, heartbeat timestamps for long
scrapes, admin cross-tenant view audit.

Windows/Codex invocation notes: do NOT pass -s read-only; keep -c mcp_servers={}
+ --skip-git-repo-check; pipe through grep -a; never git diff (feed code inline);
pass < /dev/null so codex exec doesn't hang on stdin.

START WITH PHASE 0. Run the prod query read-only and report back before anything else.
```
