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

---

## Review (2026-09-04)

### Changes
| File | Change |
|---|---|
| `src/scrapers/pierce_wa_probate.py` | Grid identified by row SHAPE (`_own_rows`, `_is_grid_row`, `_is_grid_signature_row`, `_ARMS_MIN_ROW_CELLS`) instead of `len(rows) < 5`. |
| `src/api/routes/batches.py` | A batch child that is not `done` reports `record_count=0`. |
| `tests/test_pierce_arms_small_result_page.py` | New. 14 tests; 7 fail on `origin/main` with the production error. |
| `tests/test_batches_read.py` | +1 test (`partial_batch` fixture); fails on `origin/main` with `assert 210 == 0`. |
| `scripts/diag_test11_repro.py`, `scripts/diag_test11_rowthreshold.py` | Reproduction harnesses. |

### Verification
- Exact failing range `06/04–09/01/2026`: **raised on page 10 before, 222 records after**.
- A/B `06/04–09/02/2026`: **223 records before and after** — zero regression.
- Live: 05/26, 12/26/2025, 09/02 now extract; a genuine 0-record day still returns 0.
- `ruff check src/ tests/` clean. Full suite **2260 passed, 2 skipped**. CI green on #217.
- Prod UI captured with Chromium: "Completed with errors", failed child "210 leads".

### Codex gate
| Round | Result |
|---|---|
| Consult (pre-implementation) | 6 findings; all verified independently, 2 became fixes, 1 declined with reasoning |
| Diff review r1 | **2 × P1** — chrome table could score a blocked page as healthy 0; zeroing a non-done child hides rows that reach the combined CSV. Both fixed. |
| Diff review r2 | **1 × P1** (`_retry_scrape_job` resets `record_count` to 0, so `min()` still hides rows) + 2 × P2. All fixed. |
| Diff review r3 | ⏭️ **NOT RUN** — CLI usage limit again (resets 10:27). Its two questions were self-verified: `is_duplicate` is `nullable=False` so `IS NOT TRUE` ≡ `= FALSE`; `clean()` only strips control chars and collapses whitespace, so it can never create digits and the signature can only be more permissive than `_map_row`, never reject a real row. |

### Open
- ⏭️ **#217 not merged** — user holding. In-product re-run of Test 11 therefore UNVERIFIED.
- ⏭️ Same-family row-count guards in 4 other scraper templates: reported, not changed.
- 📋 A non-done child's count is *non-duplicate saved rows*. The combined CSV additionally
  applies actionability, the tax cap and dedup buckets, so it can still sit slightly above what
  downloads — an approximation, never a fabrication. The accurate delivered number is the
  batch-level `combined_record_count`, which reads the run's `delivery_counts`.
