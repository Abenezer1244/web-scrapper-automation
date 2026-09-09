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
---

# Session 2026-09-09 — the two P1s closed, and billing turned on

Continues the audit above. The two P1s section 6 of the handoff left open are done,
plus the eight-then-three findings the review gate raised against the fixes.

## P1-b — plan switching (`fa8097b`)
- [x] `POST /billing/change-plan` via `stripe.Subscription.modify`
- [x] Licensed and metered items move in ONE array (Stripe requires one interval
      per subscription, so they cannot move separately)
- [x] Metered item REPLACED, never re-priced in place, so `assert_billable`'s
      `created <= usage_at` check still refuses this period's earlier lookups
- [x] Shares checkout's advisory lock namespace (4243) — one invariant between them
- [x] `incomplete`, `past_due` and `unpaid` refused, each with its own next step
- [x] Subscription shape validated BEFORE the same-plan shortcut
- [x] Frontend wired (`83e5e0b`): checkout's 409 opens a confirmed switch

## P1-a — billing the overage (`46842a5`)
- [x] Migration 093 `provider_submitted_at`, written once with known provenance
- [x] `provider_submitted_time()` is pure and tested per path; adoption with no
      provider timestamp yields NULL, never the adoption clock
- [x] `claim_time` (pre-POST, pre-commit) threaded through dispatch and retry
- [x] `USAGE_PROVENANCE_IS_TRUSTWORTHY = True`
- [x] The gate moved INTO the sender, bound to the arguments it sends
- [x] Migration 094 for databases that already ran the old 092

## Gate rounds
- [x] Round 1 (uncommitted work): 1 P1 + 5 P2 + 1 P3 — all fixed
- [x] Round 2 (`46842a5`+`fa8097b`): FAIL, 5 P1 + 2 P2 + 1 P3 — all fixed in `f5d0a02`
- [x] Round 3 (`f5d0a02`): FAIL, 1 new P1 + 2 P2 + 2 partials — fixed in `db3068f`
- [ ] Round 4 on `db3068f` — NOT RUN. Three gate rounds found something every time,
      so treat "no round 4" as unverified rather than clean.
- [ ] Frontend gate on `83e5e0b` — started, killed by the machine running out of
      memory before it emitted a verdict. Not run.

## Still open
- [ ] Neither branch pushed; BE PR #268 still a draft; FE PR would be new.
- [ ] **Merge backend FIRST.** The frontend calls an endpoint that is not on `main`,
      and its `api-types.generated.ts` regen needs the backend schema there.
- [ ] FE `lib/api-types.generated.ts` regen after the backend merges.
- [ ] Two deliberate holds send work to a human that could in principle be automatic:
      cross-window quantity allocation, and usage stranded by a metered-item delete.
      The automatic versions (historical-window allocation; a period-end scheduled
      switch) both need Stripe sandbox verification nobody has done.
- [ ] `_metered_skip_trace_price` still logs and proceeds unmetered when an interval
      has no provisioned price. Selling the plan beats metering it, but it means an
      unprovisioned interval silently sells overage that cannot bill.

## Review
Two things are worth keeping from this session more than the code.

The first is that **turning the switch on was never a one-line change**, and the
shape of the work only became visible after asking why the switch existed:
`assert_billable` refuses a NULL `usage_at` independently of the flag, and nothing
wrote a `usage_at`. Flipping it alone would have changed nothing at all.

The second is that **the review gate earned its place three times**, and twice it
caught me asserting something I had not checked. `46842a5`'s commit message claims
the dispatch timestamp is "at or before the lookups"; it is not, because bookkeeping
runs after the POST returns. The `billing_proof` guard I added to close a bypass was
itself bypassable with `{}`. And the fix for P1-4 moved the defect one step later
rather than removing it. Every one of those looked right when written.

The pattern across all three: an argument about ordering or provenance that is
correct in the abstract and false about the specific line of code it is attached to.
The defence is not more care while writing the sentence, it is checking the sentence
against the code afterwards — which is exactly what the second reviewer did.
