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

## Phase 1 — Backend (this branch)

- [x] Verify root cause against prod data
- [x] Consult Codex on design
- [ ] Add `derive_batch_child_name()` helper: normalize whitespace, strip control chars, cap 255
- [ ] Use it in `create_batch`; keep legacy `(batch)` fallback for nameless direct-API calls
- [ ] Tests: named batch, blank/whitespace name, control chars, over-length truncation
- [ ] `pytest` + Codex review of the diff

## Phase 2 — Frontend (`bridgeleads-web`, off `origin/master`)

- [ ] Verify + merge PR #80 (name column) and #81 (name required)
- [ ] Duplicate-only disambiguator: show `created_at` time under the name ONLY when another row in
      the list shares the name. No DB rename (user rejected a backfill).
- [ ] `tsc --noEmit` + `eslint`, verify via `next build` (repo has no test runner)

## Phase 3 — Dead code sweep

- [ ] Remove dead imports/unused locals across touched files

## Review

_(filled in at the end)_
