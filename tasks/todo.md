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
- [ ] Create three YEARLY metered skip-trace Prices in Stripe and set
      STRIPE_PRICE_SKIP_TRACE_{PRO,BUSINESS,AGENCY}_ANNUAL on api and worker.
      Annual subscriptions are unmetered until then (logged, not broken).
- [ ] Decide whether a Pro batch should keep the overlaps_only default
      (see report section 5, "Two things deliberately left")
- [ ] P2-6: the meter outbox stamps a billable event reported when the customer
      has no stripe_customer_id
- [ ] P3-2: skip-trace allowance resets on the calendar month; records reset on
      the subscriber anniversary
- [ ] Codex round 2 hit its usage limit mid-review (resets 07:35). Worth
      re-running against the final diff.

## Review
The audit found nine gaps; four were defects and five were product decisions the
owner approved in full. Codex found four of the nine independently, corrected one
of my findings (the Starter freshness delay IS implemented; two greps ending in
`| head -N` both cut before it), and caught a regression I introduced (the clamp
applied to paid plans, which shortens the forward auction horizon trustee_sale
derives from the window's length).
