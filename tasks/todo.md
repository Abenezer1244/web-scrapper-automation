# Test 11 — "Completed with errors" root cause + fix

Branch: `fix/test11-completed-with-error`  ·  Worktree: `C:/Users/Windows/bridgeleads-worktrees/test11-dq`

## What Test 11 is
- `scraper_batches` row `437a4939…` name **"Test 11"**, one `batch_runs` row `019048aa…` status **`partial`**.
- FE `app/(dashboard)/batches/[id]/page.tsx:33` maps `partial` → **"Completed with errors"**.
- Children: `Test 11 - Pierce Probate` (`caa255a9…`, **done**) and
  `Test 11 - Pierce Pre Foreclosure` (`25a8ea53…`, **failed**, retry=2).

## Confirmed root cause (reproduced, not inferred)
`src/scrapers/pierce_wa_probate.py::_extract_records` picks the ARMS results grid with
`if len(data_rows) < 5: continue`. The grid has 1 header `<tr>` + 1 `<tr>` per record, so a
page holding **1–3 records** is skipped, `data_table` is None, the record-count marker is
non-zero, and it raises `TransientScrapeError` → 2 retries → job **failed**.

Evidence:
- `06/04/2026–09/01/2026` = 228 records / 10 pages → last page has 3 rows → **exact repro**
  (fails after `page=9/10`, matching the prod row `page_current=9, page_total=10`).
- Single days with exactly 3 records (05/26, 12/26/2025, 09/02) all raise.
- Divorce `08/24–08/28` = **1 record** → also raises. Affects probate + pre_foreclosure + divorce.

## Plan
- [x] Locate Test 11 in prod; dump job/batch/job_logs
- [x] Reproduce the failure against `origin/main` code
- [x] Prove the row-count threshold is the cause; rule out other candidates
- [x] Verify the replacement table-picker on 0 / 1 / 3 / 5 / 9 / 228-record pages, 3 record types
- [ ] Consult Codex on the fix before implementing
- [ ] Implement fix + regression tests
- [ ] Full pytest, ruff, mypy
- [ ] Codex diff review
- [ ] Live re-run in prod + browser verification
- [ ] Data-quality audit of the resulting leads

## Not defects (verified)
- `08/24` "5 found / 4 extracted": row 3 dropped by the deliberate *no person party* filter
  (both parties corporate). Logged with a reason.
- Batch status `partial` is **correctly** assigned — one child succeeded, one failed.
