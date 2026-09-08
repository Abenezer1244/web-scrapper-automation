# Plan entitlement audit (2026-09-08) — COMPLETE

Branches: `chore/entitlement-audit` (BE), `chore/entitlement-audit-fe` (FE).
Report: `docs/ENTITLEMENT-AUDIT-2026-09-08.md`.
Tests: `tests/test_plan_entitlement_audit.py` (162), run via `run-audit-tests.sh`.

## Audit
- [x] Locate every plan/entitlement definition (12 sources, listed in the report)
- [x] Build the matrix from verified code and runtime behavior
- [x] Records, counties, record types, skip tracing, exports, schedules, delivery,
      batch, API, overlap, priority queue, white-label, seats, freshness
- [x] Stripe price/product mapping verified against the live account
- [x] Frontend vs backend for every capability
- [x] Codex independent review, findings verified independently

## Fixes (all four owner decisions approved)
- [x] Priority queue on scheduled + batch enqueue
- [x] normalize_plan() at every gate; plan_label follows
- [x] Starter freshness clamp, delay-only (Codex caught it clamping paid plans)
- [x] enrichment.skip_tracing mirrored onto the column the worker reads
- [x] Export-format gate (create + edit delta, FE mirror)
- [x] Schedule-frequency gate (create + edit delta, FE mirror)
- [x] Overlap gate on /segments (router dependency, FE nav + page)
- [x] Metered skip-trace price attached at checkout; licensed item resolved by id
- [x] /billing/pricing comparison derived from the matrix
- [x] Public pricing page corrected

## Verification
- [x] 2685 non-integration + 179 integration passing, isolated DB
- [x] `python scripts/export_openapi.py --check` clean
- [x] FE `tsc --noEmit` and `eslint --quiet` clean
- [x] Zero em dashes added to either repo

## Owner follow-ups
- [x] Three YEARLY metered skip-trace Prices created live and idempotently
      (price_1UDNZQ.. pro, price_1UDNZR..5SMHjj11 business,
      price_1UDNZR..SuH1gO90 agency), verified, and set on api AND worker with
      --skip-deploys. Annual checkouts now attach a metered item.
- [x] Pro batch overlaps_only: EXPLICIT is refused below Business, the DEFAULT
      coerces to "everything". Neither card becomes false.
- [x] P2-6: _MissingCustomerError is its own signal; the customer id is
      re-resolved from users, the row is HELD not written off, the sweep joins
      users so it does not hot-loop, and held rows are alerted.
- [x] P3-2: skip_trace_period_start now holds the entitlement window start.
      Same column, new meaning, no migration.
- [ ] **Codex review gate still owed.** Its usage limit has not reset (checked
      04:40 and 05:15; resets 07:35). Nothing in this batch has had a second
      reviewer. Run `codex exec` against `git diff origin/main` when it is back.
- [ ] Neither branch is pushed and no PRs are open.
- [ ] A Business account that downgrades keeps batches with overlaps_only
      STORED, and re-runs still deliver overlaps. That is the same
      grandfathering the edit enable-delta uses everywhere else, but it is a
      choice, not an accident.

## Review
The audit found nine gaps; four were defects and five were product decisions the
owner approved in full. Codex found four of the nine independently, corrected one
of my findings (the Starter freshness delay IS implemented; two greps ending in
`| head -N` both cut before it), and caught a regression I introduced (the clamp
applied to paid plans, which shortens the forward auction horizon trustee_sale
derives from the window's length).
