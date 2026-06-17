# Watchdog dup fix — complete the deferred follow-ups (Codex-reconciled)

**Session 2026-06-17.** Builds on PR#57 (70-min stopgap). Goal: permanently kill the
watchdog re-queue duplication bug and make long enrich jobs safe.
(Prior session's plan — FEK drift + King filter re-test — is COMPLETE; see memory
`project_session_handoff_2026_06_16_taxcap_and_fek_drift`.)

## Session 2026-06-17 - cleanup script stall fix
- [x] Query graph/build context and read the latest build-journal entry.
- [x] Attempt required Codex pressure-test before code changes; blocked by sandbox
  `EPERM` resolving `C:\Users\Windows`.
- [x] Restructure `scripts/cleanup_watchdog_dup_results.py` so `--commit` uses
  short transactions: committed anchor repoint first, then committed delete batches.
- [x] Add production rerun safety checks for terminal jobs and exact batch rowcounts.
- [x] Verify with compile/lint equivalents and record the review/build-journal notes.

## Live job (handled, in background)
- King job `1a54d04e` — enriching, single-copy, clean. GIS sweep stalled (mailing
  stuck 140/24708 for 13+ min). Protective monitor `monitor_king_job_guard.py`
  (bg task `bcfnqx07x`) CAS-cancels at 63 min to prevent the 65-min-kill / 70-min-requeue
  dup. Verification already stands on the clean snapshot. ⬜ confirm monitor cancelled cleanly.

## Codex consult — reconciled (see `.context/codex_idempotency_design_consult.md`)
Codex (gpt-5.5, high effort) reframed the design. Adopted in full:
- ❌ My original "DELETE prior results + reverse billing on retry_count>0" = too destructive,
  weakly fenced (concurrency race on claim release, crash-after-delete erases recoverable
  state, retry_count is not a safe trigger, cross-tenant system-role DELETE grant is dangerous).
- ✅ Instead: **make `results` inserts idempotent per job** (deterministic fingerprint +
  `UNIQUE(job_id, fingerprint)` + `ON CONFLICT DO NOTHING`). A re-run can't append dupes.
  No DELETE grant, no billing reversal, no destructive cleanup for the normal path.
- ✅ This + the existing "skip already-enriched" filter (`not res.mailing_address`) turns a
  Celery hard-kill→requeue into a **safe resume** that makes forward progress.
- ✅ Heartbeat in a **separate short transaction**, per-chunk not per-record, never commits
  the main work txn.
- ✅ Billing idempotency via stored `jobs.billed_count` + `billing_applied_at` guard
  (bill once), guarded reversal that fails loud (no silent negative clamp).

## Phases (each ≤5 files, verify + check-in between)

### Phase 1 — Heartbeat + watchdog (stops FALSE re-queue of live jobs) ✅ CODEX-CLEAN
- ✅ Migration 061: `jobs.last_heartbeat_at TIMESTAMPTZ NULL`
- ✅ `models.py`: column
- ✅ `status.py`: `HeartbeatThread` (daemon, OWN short txn, attempt-scoped via started_at,
  self-reap + 75min cap, context-manager so __exit__->stop() on every exit incl. exception)
- ✅ `tasks.py`: `with HeartbeatThread(job_id) as _hb, rls_sync_session(...) as db:`; `_hb.start(job.started_at)`
  after claim; claim UPDATE also sets `last_heartbeat_at=_now()` (closes stale-retry race)
- ✅ `health.py`: active-job re-queue gated on `last_heartbeat_at < now()-15min`, conservative
  `started_at>70min` fallback for NULL-heartbeat. Zombie/orphan branches kept.
- ✅ Codex: 2 rounds, both Highs (uncaught-exception pin; stale-retry race) + Medium + Low fixed.
- ✅ Regression tests (tests/test_workers.py): fresh-heartbeat long job left alone; stale-heartbeat
  re-queued; _write_heartbeat attempt-scoping.

### Phase 2 — Idempotent result inserts (stops DUP on re-run) ✅ CODEX-CLEAN
- ✅ Fingerprint = `rec.raw_html_hash or _source_fingerprint(rec)` (SHA-256 over canonical
  scrape-time tuple; EXCLUDES enrichment_data/mailing_address so it can't drift on re-run).
- ✅ VERIFIED: King tax one-record-per-parcel (unique); template scrapers set raw_html_hash → no regression.
- ✅ Migration 062: `results.source_fingerprint TEXT` (no backfill — legacy rows terminal).
  Out-of-band `scripts/create_result_fingerprint_index.sql` (partial UNIQUE CONCURRENTLY + validity gate).
- ✅ `models.py`: column + partial unique index (for create_all/tests).
- ✅ `tasks.py`: `pg_insert(...).on_conflict_do_nothing(index_elements=[job_id,source_fingerprint], index_where=...)`.
  Dedup Step 2b: union `first_job_id=job` owned claims so re-run doesn't mark all is_duplicate.
- ✅ Codex: 2 rounds; H1 (unstable fingerprint) + H2 (bill from len) fixed.
- ✅ migration 062 hardened: inline-build only on small (<50k) table, RAISE on large w/o
  out-of-band index, indisvalid+indisready validity gate (Codex final review).
- ✅ Test (tests/test_workers.py): re-run same (job_id, fingerprint) via ON CONFLICT → 0 appended.

### Phase 3 — Idempotent billing (bill once per job across retries) ✅ CODEX-CLEAN
- ✅ Migration 063: `jobs.billed_count INT NOT NULL DEFAULT 0` + `jobs.billing_applied_at TIMESTAMPTZ NULL`
- ✅ `models.py`: columns
- ✅ `tasks.py`: CAS `UPDATE jobs SET billing_applied_at=now() WHERE billing_applied_at IS NULL`;
  only on rowcount 1 increment records_used; billable_count from PERSISTED non-dup rows (not len);
  User-update rowcount!=1 → rollback + fail loud.
- ✅ Codex: folded into P2 review.
- ✅ Test (tests/test_workers.py): billing CAS applies once across re-runs; records_used charged once.

### Final integrated Codex review (full Phase 1-3 diff) ✅
- 2 Highs fixed: migration 062 validity gate (indisvalid+indisready, not name-only);
  idempotency test now uses the exact partial-index conflict target (index_where).
- HeartbeatThread.start() made idempotent (no double-start thread leak).
- All py_compile + ruff clean. Tests validated in CI (local pytest hits PROD Supabase/Upstash —
  DATABASE_URL/REDIS_URL are prod; do NOT run locally).

### ⚠️ DEPLOY ORDER (Phases 1-3 ship as ONE PR — Codex deploy-atomicity note)
1. migrations 061+062+063. 2. build `uq_results_job_fingerprint` CONCURRENTLY out-of-band.
3. THEN deploy worker code (ON CONFLICT needs the index to exist). models.py has source_fingerprint
   so it MUST not deploy before migration 062.

### Phase 4 — Resumable GIS sweep ✅ CODEX-CLEAN (commit 90287b6)
- ✅ Investigated live (`scripts/diag_king_gis_latency.py`): King has NO `_KNOWN_GIS_ENDPOINTS`
  entry → falls to WA statewide fallback. Endpoint HEALTHY: ~2s/chunk, 46/50 hits, returns
  property+mailing. Stall was STRUCTURAL: sweep committed ONCE at the end (no progress persisted,
  mid-sweep kill lost everything, resume restarted the whole sweep).
- ✅ `enrich.py`: commit per 500-parcel batch + progress logs. Filled rows survive a kill;
  `results_need_addr` excludes them on resume → converges. Commit-failure = rollback+warn+continue
  (never rollback-then-empty-commit); enrichment stays best-effort per caller's try/except.
- ✅ Codex 2 rounds: rollback-then-empty-commit High closed; best-effort contract documented.
- 📌 Follow-up (deferred, bounded): ~8% GIS-miss rows re-queried each resume — would need a
  persisted "GIS-attempted" flag. Does NOT block convergence.

### Phase 5 — Historical dup cleanup (#2) ✅ SAFE SUBSET DONE
- ✅ `scripts/cleanup_watchdog_dup_results.py` (content-fingerprint dedup, dry-run default,
  Codex-reviewed: caught a Critical fingerprint bug — combine raw_html_hash WITH the tuple, not
  choose; jsonb_build_array encoding; recompute-in-admin-txn; delivered_records anchor assertions).
- ✅ **8 clean King-tax watchdog-victim jobs deduped → exactly one row per parcel** (c0cf081a,
  6a91ee26, 790427cd, a99b8eca, 2ebd297d, 14ddd100, bc4504b2, 13309f57). ~236,722 rows deleted,
  all `is_duplicate=true` → ZERO billing impact. 0 anchor re-points (King tax has no dedup_hash).
- 🔧 STALL INCIDENT + FIX (Codex consulted + coded): first run stalled `idle in transaction` 15min
  on a 76,992-row single-job transaction. Terminated the backend (safe rollback, no corruption,
  no stuck locks), Codex restructured to SHORT COMMITTED BATCHES (re-point→assert→delete in 500-row
  committed chunks w/ exact-rowcount checks). Re-ran clean (DEBUG=false, no grep pipe) → 8/8 done.
- ⏭️ DEFERRED: the ~29,395 `is_duplicate=false` (BILLED) dup rows across King-tax x6 (a988b776) +
  probate/spokane jobs — the script REFUSES these (billing implication). Needs a billing-aware pass
  that also decrements `records_used`. NOT done.

## Gating
- Codex reviews the diff after each phase (`codex review` / `codex challenge`).
- Security Master Review after the migration/grant-touching phases.
- Any Critical/High from either reviewer = NO-GO until fixed.

## Open flag (not this task)
- ⚠️ Saw ONE `InvalidToken: fe1:-prefixed value not decryptable under strict mode` in worker
  logs at 03:48 UTC. Single line, not a flood; King job enriching fine (retry_count=0).
  Handoff says FEK drift resolved — could be a stale row read. Watch for recurrence.

## Review
Cleanup script now commits `delivered_records` anchor repoints before deleting, then
deletes `results` in committed 500-row batches with a per-batch anchor assertion and
exact rowcount check. Added a commit-time terminal-job guard. Root-cause call:
client-side blocking while an open transaction sat idle is more likely than a slow
active cascade, because Postgres reported `idle in transaction`; confirm from
`pg_stat_activity.wait_event/client_addr/query_start/state_change` plus Python stdout
stack/strace next time. Verification: `py_compile`, script-level `ruff`, and `--help`
passed. Full `ruff check .` still fails on pre-existing unrelated files.
# Hardcoded secrets audit and remediation plan

**Session 2026-06-17.** Goal: find hardcoded credentials in current files and git
history, remediate real code-level leaks by moving them to env vars, harden ignore
rules, and identify secrets that need immediate rotation. Values in reports will be
redacted; raw secret values will not be echoed into chat or committed to docs.

## Scope
- Current working tree: tracked and untracked files, with generated caches/venvs
  excluded from broad scans but explicitly checking suspicious tracked artifacts.
- Git history: all reachable branches/tags. If stashes/reflogs are available, note
  whether they were checked or why not.
- Secret classes: API keys, DB/Redis URLs, JWT/session secrets, S3/R2 credentials,
  Stripe, Resend, Supabase, GitHub/OpenAI-style tokens, passwords, private keys,
  browser storage/cookie state, deployment config, test fixtures, logs, exports,
  dumps, and archives.

## Phase 1 - Evidence inventory only
- [ ] Run dedicated secret scanner if available (`gitleaks` preferred; fallback to
  custom `rg`/`git grep` patterns if unavailable).
- [ ] Scan current tracked files, untracked files, and tracked ignored files.
- [ ] Scan all reachable git history with batched revision handling to avoid command
  length limits.
- [ ] Classify each hit as real secret, placeholder/example, non-secret config, or
  false positive.
- [ ] Produce a redacted inventory with path, line, commit hash if historical,
  credential type, confidence, and current vs historical status.

## Phase 2 - Remediation, max 5 files before check-in
- [ ] For real current code secrets, update code to read from settings/env with
  fail-closed validation and no production defaults.
- [ ] Add required placeholder names to `.env.example` only, never real values.
- [ ] Harden `.gitignore` for env variants, local credential files, browser state,
  traces/videos/screenshots, exports, dumps, logs, and local audit outputs.
- [ ] Re-scan after edits to confirm current-file leaks are gone.

## Phase 3 - Verification and review
- [ ] Run project type-check equivalent, or state explicitly if none exists.
- [ ] Run configured lint equivalent.
- [ ] Run a final redacted secret scan over current files.
- [ ] Run Codex review/challenge on the remediation diff.
- [ ] Append a review section here and a session entry to `docs/BUILD_JOURNAL.md`.

## Risks and notes
- Historical secrets stay compromised after code changes; rotate/revoke any confirmed
  real credential found in current files or git history.
- Git history rewrite is a separate decision. If real secrets are in shared history,
  rotation is mandatory; history rewrite may also be needed with coordinated force-push.
- Existing dirty worktree changes are unrelated and must not be reverted.

---
