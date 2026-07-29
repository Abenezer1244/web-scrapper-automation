# Scraper name disambiguation — dashboard Scrapers widget

**Branch:** `fix/batch-child-scraper-names` (BE, off `origin/main`)
**Worktree:** `C:/Users/Windows/bridgeleads-worktrees/xcheck-0729`
**Reported:** the dashboard Scrapers table has no name column, so rows are indistinguishable and
you can't tell which row's "View" to click.

> Separate file (not `tasks/todo.md`) because that file holds another session's in-flight
> trustee-sale plan.

## Verified facts (not assumptions)

Prod read-only query (`scripts/diag_scraper_name_collisions.py`), 29 configs:

- 12 configs (41%) collide on name, all inside ONE user's list.
- Every collision is a **batch child**: `King pre_foreclosure (batch)` x3, `King probate (batch)` x3,
  `King tax_delinquent (batch)` x2, `Pierce pre_foreclosure (batch)` x2, `Pierce probate (batch)` x2.
- **0 configs** carry the frontend wizard's `${county} ${record_type}` default — so the wizard
  fallback was never the real source. FE PR #81's premise is hardening, not the fix.
- Within each colliding group `created_at` is unique 100%; `schedule` only 2/3; `doc_types` 0/3.
- Same-day, minutes-apart timestamps exist (Jul 3 23:54 vs 23:57) → date-only is insufficient.

**Root cause:** `src/api/routes/batches.py:256` names each child
`f"{county.title()} {rt} (batch)"`, discarding `body.name` (the batch name the user typed, stored on
the parent at line 228). Every batch over the same county+record_type yields identical children.

## Codex consult (pre-build) — adopted

1. Cap/normalize the DERIVED child name, not just the input (county col is 128 → 120+3+128 > 255).
2. Treat whitespace-only `body.name` as absent.
3. `delivery.py:74` puts raw `scraper_name` in the email **subject** (`html.escape` only guards the
   HTML body) → strip control chars at the source so every consumer benefits.
4. UI disambiguator must render time, not just date.
5. Naming is identifier UX, not a uniqueness guarantee — keep routing by id.

## Phase 1 — Backend (this branch) — DONE, **PR #159**

- [x] Verify root cause against prod data
- [x] Consult Codex on design
- [x] Add `derive_batch_child_name()` helper: normalize whitespace, strip control chars, cap 255
- [x] Use it in `create_batch`; keep legacy `(batch)` fallback for nameless direct-API calls
- [x] Tests: named batch, blank/whitespace name, control chars, NBSP, over-length truncation (15)
- [x] Full suite 1642 passed / 2 skipped / 0 failed + Codex review PASS, no findings

## Phase 2 — Frontend (`bridgeleads-web`) — DONE, **PR #89**

- [x] Verified + merged PR #80 (name column) and #81 (name required)
- [x] Duplicate-only disambiguator: `created_at` timestamp under the name, only when another row
      in the account shares the name. No DB rename.
- [x] Fixed a contradiction #81 shipped (Codex P2): the field still read "Name this batch
      (optional)" while the schema rejected blanks
- [x] `tsc` 0 / `eslint` 0 / `next build` clean; strings confirmed in the emitted bundle
- [x] Codex re-review PASS, no findings

## Phase 3 — Dead code sweep

- [x] `ruff` (incl. F401/F841/ARG/ERA) clean on all touched backend files — one real hit fixed
      (unused loop var in the new diag script)
- [x] `eslint` clean on all touched frontend files
- [ ] NOT done repo-wide: ~277 outstanding `I001` import-order findings would collide with the
      other sessions' in-flight branches. Left for a quiet moment on a dedicated branch.

## Review

**What changed.** The reported symptom was a missing name column. The column already existed as
unmerged PR #80 — but merging it alone would have rendered duplicate names twice, because the
real defect was upstream: `batches.py:256` threw away the batch name when naming children. 41% of
production configs collided as a result. Backend now derives distinct child names; the dashboard
disambiguates the 12 rows that already collided.

**Notes / residual risk.**
- Existing duplicate names are untouched by design (no backfill). They remain ambiguous in
  surfaces the dashboard hint doesn't reach — job logs, in-app notifications, per-job email
  subjects, dialer/webhook metadata. Codex flagged this; it needs a rename to fully close.
- Two batches may still share a name, so the new formula can still collide. That is acceptable:
  routing is by id and the UI disambiguates equal names.
- Codex predicted the `"(batch)"` literal would break tests. It did not — those strings are
  hand-built fixtures, not assertions on generated names. Verified by the full suite.
