# Mailing follow-ups after #283 (2026-09-13)

Branch `feat/mailing-followups` (worktree `C:/Users/Windows/bl-wt-rpacct`, from `origin/main` @ `a55308f`).
Owner approved all follow-ups, turning the Snohomish/Cowlitz restricted mailing flag on, and a UI check.

## Done without code
- [x] #286 journal merged `a55308f`
- [x] `COUNTY_GIS_RESTRICTED_MAILING_ENABLED=true` on worker + api (deploys 42aeeba1 / 267fe7ff SUCCESS); prod env reports Snohomish + Cowlitz as mailing sources; 0 rows created while the flag was off
- [x] Snohomish Test 5 (job 425d49ce, 4 rows): county taxpayer mailing equals the stored value for all 4. No write: Snohomish GIS rows never carry `mailing_source`, so these are now indistinguishable from sourced rows and confirmed

## Phase 1 (code, <= 4 files)
- [ ] A. Backfill candidates skip rows whose latitude/longitude are JSON null or empty (same rule as the live hook), so the 15 unlocatable rows stop being revisited
- [ ] B. Auto skip trace does not enqueue code_violation leads whose complaint status is `Completed` or `Open Duplicate` (log a count, row stays `not_attempted`, like the placeholder gate)
- [ ] Tests for A and B; Codex consult before, review after; CI; merge; deploy

## Phase 2 (script + prod run)
- [ ] C. `scripts/king_taxbill_mailing_verify.py`: King's live tax bill for (1) 149 situs-echo rows the extract could not answer (90 ambiguous / 57 absent / 2 no_address) and (2) 124 matched code-violation PINs with no extract answer (89 absent / 35 ambiguous). Uses `batch_enrich_king_county` (shared source lease, identity gate, circuit breaker) at a gentle pace. Guarded writes on done jobs: echo row found -> replace (or confirm) with `mailing_source=king_tax_bill`; CV row found -> fill. `none`/error -> unchanged. Dry-run first
- [ ] D. Results page UI check with Playwright (owner supplied a login; never stored)

## Review
(pending)

---

# Single-user 3-month Agency promotion through Stripe

Branch: `feat/stripe-promo-access` · worktree `C:/Users/Windows/bridgeleads-worktrees/stripe-promo` (from `origin/main` @ `ff9ecd6`, includes #268)

## Findings (current architecture, read from origin/main)

| # | Question | Answer | Where |
|---|---|---|---|
| 1 | Checkout Session creation | `POST /billing/checkout`: advisory lock 4243, re-read user, resolve/adopt customer, **refuse if any non-terminal subscription exists**, expire open sessions, re-check, then `Session.create(mode="subscription")` with the plan price + metered skip-trace price | `billing.py` `create_checkout` |
| 2 | Subscription creation | Only via Checkout. `/billing/change-plan` modifies, never creates | `billing.py` |
| 3 | Customer id storage | `users.stripe_customer_id`, persisted after the Session exists; bound again in `checkout.session.completed` if NULL; mismatch refuses | `billing.py` |
| 4 | Subscription id storage | `users.stripe_subscription_id`, written by `activate_paid_plan` / `apply_plan_change`, cleared by `end_subscription` | `billing_entitlement.py` |
| 5 | Price to plan mapping | `_PRICE_TO_PLAN` from `PLAN_CATALOG` (monthly + annual ids). Licensed item found via `_plan_item_price_id` | `billing.py`, `plans.py` |
| 6 | `checkout.session.completed` | Retrieves the subscription, maps the plan price, locks user `FOR UPDATE`, `activate_paid_plan(status=subscription.status)`. **Never reads `payment_status`, amount, or PaymentIntent** | `billing.py` |
| 7 | Subscription webhooks | `updated` re-reads from Stripe, `apply_plan_change`; `deleted` downgrades to Starter. **`customer.subscription.created` is not handled** | `billing.py` |
| 8 | $0 invoices | No amount checks anywhere in `src/`. `invoice.payment_succeeded` only clears dunning | grep: no `amount_*`/`discount`/`payment_status` reads |
| 9 | Promotion codes in Checkout | **Already enabled**: `allow_promotion_codes=True` on every checkout (founding coupon `FOUNDING25` exists) | `billing.py` |
| 10 | Discount affects entitlement? | **No.** Plan, records limit, county limit, record types all derive from `users.plan`, which derives only from the Price | `constants.py` |
| 11 | Monthly vs annual | Both map to the same plan; entitlement window is always monthly; interval switch through `change-plan` sets `billing_cycle_anchor="now"` | `billing.py` |
| 12 | Entitlement audit (#268) | Added the second-subscription guard, `change-plan`, anchor/quota rules. The promo must go through that guard, not around it | merged |

### Stripe behaviour verified against docs.stripe.com (billing/subscriptions/coupons)
- `duration=repeating, duration_in_months=3`: monthly subscription gets the first 3 invoices discounted.
- **Yearly subscription: "the discount applies to the entire year if the subscription renews within the N-month window."** A 3-month 100% coupon on Agency annual = a free year ($14,390). This is the conflict to guard.
- Promotion codes support `customer` restriction, `max_redemptions`, `expires_at`; coupons support `applies_to.products`, `max_redemptions`, `redeem_by`.
- Customer portal plan switching is disabled on this account (per code comment), so the portal cannot re-apply codes.

### Risks found
- R1 (High): annual Agency checkout with the 3-month code = full year free.
- R2 (High): an Agency monthly promo holder calling `change-plan` to annual inside the 3 months issues an annual invoice inside the discount window = full year free.
- R3 (Medium, pre-existing): webhook Redis dedup key is claimed BEFORE handling and never released on failure. If `checkout.session.completed` raises (e.g. Stripe retrieve blip), Stripe's retry is skipped as "already processed". A $0 subscription created active may never send a later `updated`, so the customer stays unactivated.
- R4 (Medium): a coupon without `applies_to` would also zero the metered skip-trace overage for 3 months. Mitigated by config (`applies_to = Agency product`), since skip-trace is its own product.

## Plan

### Phase 1 (code, 3 files) - DONE, 27 tests, every guard mutation-proven
- [x] Checkout: annual sessions NEVER offer the code box (`allow_promotion_codes = interval != "year"`); monthly unchanged; `payment_method_collection="always"` explicit. (First built as a per-customer promotion lookup; replaced after Codex r1 showed it was a check-then-act race: a code issued while the Session is open still worked.)
- [x] change-plan: 409 `promotional_pricing_active` on a switch TO annual while a repeating (<12 mo) discount is still running on the subscription; checked before any side effect.
- [x] Webhook claim: `processing` (300s) until handler + `db.commit()` succeed, then `done` (3 days); released on exception; an in-flight duplicate gets 409 so Stripe retries instead of treating it as delivered. No migration.
- [x] `checkout.session.completed`: activates only an `active`/`trialing` subscription.
- [x] `customer.subscription.created`: routed through the update handler, gated on the RE-READ status; a failed re-read raises (retry), never trusts the body.
- [x] `customer.subscription.updated`: an UNRECORDED subscription changes nothing unless `active`/`trialing` (was: an `incomplete` Agency sub ranked as an upgrade). The recorded subscription keeps every transition.
- [x] `customer.subscription.deleted`: always asks Stripe for a surviving active/trialing subscription on a sold price; rebinds to it through the update handler instead of downgrading; list failure raises (retry).
- [x] Ops alerts: annual subscription carrying a short repeating coupon; checkout completing while a different recorded subscription is still live.
- [x] `scripts/stripe_single_customer_promo.py`: args only, refuses live key without `--live`, verifies customer `metadata.user_id`, refuses customers with a live subscription, validates an existing same-code promotion's shape, idempotency key covers every material parameter.
- [x] ruff (CI pin 0.15.6) clean; no em dashes added.

Codex r1 (post-build) rejected/deferred with reasons: change-plan read-then-modify race (only our operator can attach coupons; portal plan updates disabled); durable webhook ledger / claim lease (needs a migration and a webhook redesign the owner ruled out; reported as risk).
Codex consult rejected (false on the code): email-only customer adoption; missing signature verification.

### Phase 2 (Stripe TEST mode, needs a `sk_test_` key)
- [ ] Test clock customer, Agency monthly Checkout with the code: $0 due, subscription active, entitlement Agency/-1/-1.
- [ ] Second redemption refused; different customer refused; annual Checkout shows no code box for the holder.
- [ ] Replay the real signed events: no quota reset, no duplicate.
- [ ] Advance clock past 3 months: discount removed, invoice 4 charges $1,499, plan unchanged, no reset.
- [ ] Cancel: `deleted` downgrades normally. Checkout again while active: 409.

### Phase 3
- [ ] Codex diff review + Master Security Review §14; review section below; journal entry.

## Decisions to confirm with the user
1. Skip-trace overage during the 3 months: billed normally (recommended, `applies_to` Agency product only) or free?
2. Code redeem-by window (recommended 14 days).
3. Does the recipient already have a BridgeLeads account? A Stripe subscription?
4. A Stripe test-mode secret key for Phase 2.

## Review
**Phase 1: done and gated.** Codex consult + 6 review rounds; final round GATE: PASS. Every finding was
checked against the code before adoption (2 rejected as false). 32 promo tests, each guard
mutation-verified. Full CI target 2899 passed / 2 skipped / 0 failed; billing integration 145 passed;
ruff (0.15.6) clean; no em dashes added.

Files: `src/api/routes/billing.py`, `scripts/stripe_single_customer_promo.py` (new),
`tests/test_promo_access.py` (new), `docs/BUILD_JOURNAL.md`, this file. Nothing committed.

**Phase 2: NOT run.** No Stripe test key was provided (`C:/Users/Windows/bl-testenv/stripe-test.env`
absent). Verifier ready in the session scratchpad. Do not create anything in live Stripe until it passes.

Open for the owner: FOUNDING25 is no longer enterable on annual checkout; enable
`customer.subscription.created` on the webhook endpoint; verify the endpoint API version.
Remaining risks: no durable webhook ledger (claim lease 300s, commit ambiguity); Session-create race
without idempotency key (pre-existing; ops alert added); change-plan vs. operator Dashboard coupon race.

### Phase 2 (owner follow-up, same day): FOUNDING25 kept, risks closed, Stripe sandbox verified
- [x] Annual app gates removed; single-customer coupon scoped to the Agency MONTHLY product (script refuses products with yearly prices, reads `applies_to` back). Codex consult: FAIL -> reconciled PASS.
- [x] Durable webhook ledger (migration 095 + RLS grants/policies mirrored in provision/cutover/force SQL; exercised under real roles in a rolled-back transaction).
- [x] Post-create checkout re-check; legacy plan price aliases; invoice webhooks on API 2025-03-31+ (`parent.subscription_details`).
- [x] Codex post-build PASS + 2 delta PASS. Suite 2909 passed / 2 skipped; billing integration 145 passed / 10 skipped.
- [x] Stripe sandbox e2e: 43/44 pass, 0 fail, 1 skipped (test clock needs the sandbox claimed).
- [ ] 3-month test-clock run (owner: claim sandbox `acct_1UF5cBIoeMQyAQ5z` before 2026-09-20).
- [ ] LIVE (owner approval): Agency monthly product + price, env swap + `STRIPE_LEGACY_PLAN_PRICES`, enable `customer.subscription.created`, run the script with `--live`.

### Phase 3 (2026-09-13, branch `chore/stripe-followups`): handoff section 9 step 1
- [x] FOUNDING25 promotion code created LIVE (owner approved): `promo_1UFBJtHE9wT1C7yZS3O21fcH` on coupon
      `FOUNDING25`, no customer/expiry/code cap (coupon cap 25 is the limit). Read back active; re-run reports EXISTS.
      Script `scripts/stripe_founding_code_and_webhook_events.py` (dry run by default, `--apply --live`).
- [x] `invoice.payment_succeeded` NOT enabled (owner had approved; withheld on review). Codex consult FAIL (3 High
      on `_handle_payment_succeeded`: stale late event clears dunning / writes `active` without a Stripe re-read,
      no `FOR UPDATE` against `invoice.payment_failed`, overage invoices on the same subscription count as plan
      payment). Reconciled: recovery already clears dunning via `customer.subscription.updated` ->
      `apply_plan_change` (billing_entitlement.py:265-269), so the event is redundant today. Codex agreed; PASS for FOUNDING25 only.
      Codex's Medium (`coupon=` removed on clover) withdrawn: stripe 11.4.0 pins `2024-12-18.acacia`.
- [ ] Follow-up before ever enabling `invoice.payment_succeeded`: harden both invoice handlers (row lock,
      Stripe subscription re-read, plan-line vs overage invoice). Same stale-event gap exists today in
      `_handle_payment_failed` (writes `past_due` without a re-read).
- [ ] 3-month test-clock run: owner has NOT claimed sandbox `acct_1UF5cBIoeMQyAQ5z` yet (deadline 2026-09-20).
- [ ] After 2026-09-16: remove the Redis cutover dual-read.
