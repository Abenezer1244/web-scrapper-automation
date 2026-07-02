# Pierce pre_foreclosure run failure — 2026-07-01

## Context / trigger
Admin dashboard "Records · month" showed 0. Investigation proved:
- **NOT data loss.** Prod (owner role): 19,142 results, 52,716 delivered_records — all present.
- "Records · month" KPI = `users.records_used` (usage meter), reset to 0 at the
  July-1 calendar-month rollover (`records_period_start = 2026-07-01`, by design).
- The `diag_data_inventory.py` "0 everywhere" was an **RLS artifact** — the prod
  app role `bridgeleads_app` is now non-BYPASSRLS, so RLS fails closed to 0 with
  no user context set. (Follow-up: that diag script is now misleading — see below.)

## The real problem
Pierce `pre_foreclosure` scraper has produced **no new results since 2026-06-26**.
Today's scheduled run (07-01 05:59 UTC) scraped `record_count=170` but committed 0
and `status=failed`. DB stores only the sanitized message; real traceback logged via
`tasks.py:517 _logger.exception` (rotated out of Railway buffer).

- Failing component (VERIFIED via connector row): `PierceWAARMSScraper`
  (`src/scrapers/pierce_wa_probate.py`), base_url `armsweb.co.pierce.wa.us`.
  (NOT AcclaimWeb — the 19:40 AcclaimWeb logs were a different county.)
- **Leading hypothesis (UNCONFIRMED):** fail-loud `raise RuntimeError` at
  `pierce_wa_probate.py:469` — a day/week chunk's ARMS results page didn't render
  the record-count marker (`_record_count == "unknown"`), so the whole multi-chunk
  job raised and discarded the 170 already-scraped records.

## Root cause (CONFIRMED by reproduction)
Re-ran the exact failing config+window on the PROD worker → **SUCCEEDED** (172 records,
20 net-new billed, job done). So the June-26/July-1 failures were **TRANSIENT** (portal
hiccup), NOT a deterministic bug. Brittleness: `tasks.py` scrape handler does
`except Exception → _fail_job → return` = PERMANENT fail with no same-day retry (watchdog
only re-runs pending/stuck jobs, never failed). One flaky page = 0 leads for the day.
Re-run also repaired today's symptom: admin records_used 0 → 20.

## Codex-reconciled design (consulted; agreed)
Approach **C**: job-level retry (real fix) + page-level retry (Pierce damage reduction).
Retry policy: **3 total attempts, backoff ~5min → ~20min + jitter** (user-approved).

## Plan (all 3 phases approved)
### Phase 1 — county-agnostic reliability core ✅ DONE (py_compile+ruff clean, classifier tested)
- [x] Typed exceptions in `reliability.py`: `TransientScrapeError` (+ `ScraperBlockedError` reparented) + `is_transient_scrape_error()`.
- [x] Job-level retry helper `_retry_scrape_job` (status.py): attempt-scoped CAS reset + backoff countdown.
- [x] Wire into `tasks.py` scrape `except`: transient + attempts remaining → re-queue w/ backoff; else fail. Constants in constants.py (2 retries, 300s/1200s + jitter).
### Phase 2 — Pierce hardening
- [ ] Page-level retry (2x, 5s/15s) around ARMS result-page/pagination render; raise
      TransientScrapeError after retries (never return [] unless explicit "0 records" marker).
- [ ] Fix silent partial-success in `_go_to_next_page()` (Codex catch): if more pages
      expected and next-page nav fails after retries, RAISE transient — don't quietly stop.
### Phase 3 — tenant-filter fix (Codex catch)
- [ ] Add `user_id` to the Step-2b owned-claims query in `tasks.py` (currently filters only first_job_id).
### Verify / gate
- [ ] Idempotency verify: `uq_results_job_fingerprint` + `uq_delivered_records_user_hash`
      exist in prod; `raw_html_hash` stable across reruns.
- [ ] Codex reviews the diff — Critical/High from either reviewer = NO-GO.
- [ ] Security Master Review (§14); prove with a test; BUILD_JOURNAL entry.

## Follow-ups (separate, not this branch)
- `scripts/diag_data_inventory.py` is misleading under the non-BYPASSRLS prod role
  (counts through `system_sync_session` → RLS zeros). Should use the owner/migrate
  role or set user context. File a note so it doesn't cause a future false alarm.

## Notes
- Worktree: `.claude/worktrees/pierce-run-failure` on `fix/pierce-preforeclosure-run-failure` (off origin/main).
- All prod queries this session were read-only SELECTs (owner role for ground truth).
