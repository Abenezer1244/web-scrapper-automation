# Session: cross-check delivery build + mailing-address split columns
Branch: `chore/xcheck-delivery-build` (worktree off `feat/fields-output-visibility` @ 1311448 — separate from the other active session)

## Part A — Cross-check the delivery-step build (Q1–Q4 commits)
- [x] Read full diff origin/main...HEAD (22 files)
- [x] Claude self-review — candidate findings below
- [ ] Codex `review --base origin/main` (running in background)
- [ ] Reconcile findings, fix anything Critical/High, Codex re-verify

### Claude self-review findings (pending reconciliation with Codex)
1. **[Medium] delivery.py — SoftTimeLimitExceeded treated as permanent.** The task sets
   `soft_time_limit=30` explicitly to bound a hung Resend POST (the SDK sends with no
   timeout), but `_is_retryable_email_error()` classifies `SoftTimeLimitExceeded` as
   permanent (not a RequestException, no `code`, not `ApplicationError`) → the exact
   transient case the limit exists for is never retried. Fix: classify
   `SoftTimeLimitExceeded` as retryable.
2. **[Low] batch delivery email copy says "expires in 48 hours"** but the batch path
   sends an in-app page URL that doesn't expire (pre-existing; cosmetic).
3. Verified OK: `sa_text`/`ScraperBatch`/`_fail_job` imports; `trigger="preview"` fits
   `Job.trigger` String(32) w/ no constraint (JobCreate validator only guards POST /jobs);
   dedup-claim DELETE is tenant-scoped; upload retry bounded; rollback-then-attribute-
   access refreshes `job` safely (committed row exists).

## Part B — Feature: split mailing/property address into own columns (user request)
User: downloadable CSVs should have e.g. `10301 greenwood ave n` / `seattle` / `wa` / `98115`
as separate columns.

**Already exists:** `property_street/city/state/zip` split columns (parse_property_for_display).
**Missing:** the same for `mailing_address`.

### Plan
- [ ] 1. Add `mailing_street`, `mailing_city`, `mailing_state`, `mailing_zip` to the END of
      `LEAD_CSV_COLUMNS` (append-at-end = the file's backward-compat convention).
- [ ] 2. `build_lead_export_row`: parse `mailing_address` with the same (address-generic)
      parser; emit sanitized parts.
- [ ] 3. **Visibility dependency:** `mailing_address` is HIDEABLE — hiding it must ALSO blank
      the 4 new split columns or the hide feature leaks the mailing address. Add a
      dependent-columns map in `_apply_visibility`.
- [ ] 4. Append the 4 columns to `OVERLAP_LEAD_COLUMNS` (combined/batch CSV picks them up
      automatically via `base.get`).
- [ ] 5. Excel inherits via `_canonical_dataframe` (LEAD_CSV_COLUMNS). JSON export is raw
      record dicts — unchanged.
- [ ] 6. Tests: new-column presence, parse correctness (comma + no-comma + PO Box), hide-
      mailing blanks split cols, overlap CSV parity.

### Open design question (consult Codex)
- No-comma addresses ("10301 GREENWOOD AVE N SEATTLE WA 98115"): current parser leaves
  city blank (street/city boundary "unknowable"). User's example implies they want city
  extracted. Option A: ship parser as-is (city blank for comma-less, same as existing
  property_city today). Option B: add a conservative street-suffix heuristic
  (suffix + optional directional/unit → remaining pre-state tokens = city) used by BOTH
  property + mailing splits. Decide with Codex.

## Review
(to fill at end)
