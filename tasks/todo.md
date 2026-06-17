# Task: Resume 2026-06-16 — FEK drift recovery + King filter re-test

## Context
- 18-month tax cap SHIPPED (PR #55) + Snohomish UI test PASSED. Filter proven correct.
- BLOCKED: King re-test blocked by ACTIVE FEK-drift recurrence. Worker floods
  `InvalidToken('fe1: not decryptable under strict')` in run_scrape_job. 15 pending
  orphans (created ~05:04-06:40 UTC) re-delivered by #54 watchdog -> crash loop.

## Codex consult (DONE)
- Tax cap clean; ORM == raw SQL; no surface missing the cap.
- CRITICAL: failclean script predicate is too broad -> could kill a fresh pending
  job. Must make surgical (created_at cutoff or exact IDs) before running.
- Order: scan -> fail-clean stale ONLY (surgical) FIRST -> re-encrypt -> re-scan -> refire.
- anomaly>0 -> STOP. No restart needed (per-task decrypt fail, not boot). Verify
  Railway run uses same FIELD_ENCRYPTION_KEY as live worker.

## Plan
- [x] 1. Consult Codex on all work + findings
- [x] 2. READ-ONLY diag scan: 22 derived_hkdf in users.email, 0 anomaly, rest clean
- [x] 3. Orphans: 3 (not 15), created 05:04-06:00 UTC 06-16; exact IDs captured
- [x] 4. Confirmed: railway run --service worker => env_keys=1, derived_key_in_env=False (prod parity)
- [x] 5. Made failclean SURGICAL: --ids allowlist + --created-before cutoff; --commit
      REFUSES without a guard (Codex P1). Fixed uuid::text cast.
- [x] 6. Fail-cleaned the 3 orphans (--commit) -> 0 remaining. Watchdog reflood stopped.
- [x] 7. Re-encrypted 22 derived->primary (--apply), 0 cas_skipped
- [x] 8. Re-scan CLEAN: derived_hkdf=0 anomaly=0 (current 5619->5641)
- [x] 9. Re-fired King scrape TWICE -> both FAILED at 30s. ROOT CAUSE (measured, not
      guessed): King Socrata page query ordered by account_number,bill_year forces a
      ~67s server-side sort (cold) > 30s read timeout. AND that order is non-unique ->
      $offset paging can skip/dup the summed rows (Codex: correctness bug, HIGH).
      FIX (Codex-designed): $order=:id (2.8s, unique, stable) + per-page retries +
      settings.DEFAULT_TIMEOUT. Branch fix/king-tax-socrata-id-paging, 12 tests pass,
      ruff clean. Codex review gate: <pending>.
- [x] 10a. Restored .claude/agents/{code-reviewer,research}.md; merged PR #56 -> main
      (510a02e); worker redeployed (fresh Celery boot 01:58 UTC).
- [x] 10b. Re-fired King scrape -> FIX WORKS: scraping->enriching, 24,708 records, NO
      timeout. Job a99b8eca. (trigger's 900s poll expired during enrichment, not a fail.)
- [x] 10c. INCIDENT: watchdog re-queued the LIVE enriching King job at 21min ->
      non-idempotent re-run DOUBLED results (49,416 = 24,708 x2). Scan found same
      x2-x6 dup on prior King tax runs. Codex consulted. CANCELLED a99b8eca to stop
      the loop (cancel_job.py). User chose: core fix now + historical cleanup deferred.
- [x] 10d. ROOT CAUSE: watchdog stuck_cutoff=20min but run_scrape_job Celery hard
      time_limit=65min -> live jobs declared stuck. FIX: cutoff 20->70min (PR pending).
      DELETE-idempotency deferred (needs system-role DELETE grant on results +
      delivered_records re-point) -> folds into heartbeat redesign.
- [x] 10e. Shipped watchdog fix: PR #57 (Codex gate clean, CI green) -> merged -> deployed
      (worker boot 03:08 UTC, 70min cutoff live).
- [x] 10f. Re-scraped King CLEAN: job 1a54d04e, 24,708 rows, single_copy=YES, retry_count=0
      (watchdog fix confirmed — no re-queue, no dup).
- [x] 10g. ✅ KING FILTER VERIFIED: api_test_king_tax_filters.py 8/8 PASS (API total ==
      independent SQL ground truth). All-counties tax-filter verification COMPLETE
      (Snohomish headed-UI last session + King API this session; UI-render path proven
      on Snohomish). Verified at API layer (same /results total the headed test asserts)
      to avoid interactive browser/MFA.

## Deferred follow-ups (separate planned efforts)
- Complete fix: heartbeat-based stuck detection + retry-idempotent run_scrape_job
  (delete prior results on re-run; needs GRANT DELETE ON results TO bridgeleads_system
  in both grant files lockstep + delivered_records re-point + billing-neutral).
- Historical cleanup: dedup the watchdog-bug victim jobs (integer-multiple King tax
  x2-x6; see diag_results_dupes_all.py) with Codex survivor-selection. a99b8eca incl.
- King enrichment perf: confirm 24,708-parcel enrich completes < 65min Celery limit.

## Review
**Encryption incident: RESOLVED.** 22 derived_hkdf emails re-encrypted → primary (scan
CLEAN); 3 orphaned pending jobs surgically fail-cleaned (watchdog reflood stopped).
Hardened failclean with --ids/--created-before guard + refuse-bare-commit (Codex P1).

**King scrape: root cause found + fixed (PR #56, awaiting merge).** Not encryption — the
Socrata page query's `$order=account_number,bill_year` forced a 67s server-side sort
(>30s timeout) AND is non-unique (skip/dup paging bug, Codex HIGH). Fix: `$order=:id`
(2.8s, stable) + per-page retries + settings.DEFAULT_TIMEOUT. 12 tests pass, ruff clean,
Codex review gate PASS.

**Remaining (next session, after 👤 merges PR #56 + Railway deploys):** re-fire
trigger_king_scrape.py, then re-run ui_test_tax_filters.py on the new King job to finish
the all-counties filter verification.

**Note:** the `.claude/agents/{code-reviewer,research}.md` deletions in the working tree
are PRE-EXISTING (present at session start), unrelated to this work, and NOT in PR #56.
CLAUDE.md/AGENTS.md still reference those subagents — restore them or update the docs.
