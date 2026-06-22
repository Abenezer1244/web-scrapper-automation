# BridgeLeads — Willingness-to-Pay (WTP) Validation Playbook (2026-06)

> Companion to `docs/pricing-strategy-2026-06.md`. That doc set the **hypothesis**
> (Pro $199 / Business $499 / Agency $1,499 + ~20% annual). This doc is the
> **2-week test** to find out if it's real — before we trust it.
> Reviewed with Codex (independent model) 2026-06-21.

## Where we are
- Pricing is LIVE in billing ($199/$499/$1,499 + annual; checkout verified working).
- `FOUNDING25` coupon (25% off) is live.
- **~144 early-access accounts, 0 paying, ZERO real WTP data.** The $199 matrix is a
  defensible hypothesis from competitor research, not a validated price.
- Claude (an AI) cannot call customers. **This playbook is for the founder to execute.**

## The core principle
**Cash collected beats conversion-rate theory.** Compliments, "I'd totally pay," and
LOI "reserve my spot" replies are *politeness theater*. The only signal that counts is
a buyer completing a real Stripe checkout. Count payments, not enthusiasm.

## ⚠️ The discount trap (most important)
A 25% `FOUNDING25` coupon validates **~$149/mo effective**, NOT $199/mo. Leading with
the discount contaminates the full-price signal. So:
- **Primary offer = FULL price**, positioned as *"Founding access: priority onboarding +
  input on which WA counties we add next,"* not "25% off."
- **`FOUNDING25` is a fallback only**, offered *after* a full-price refusal — and every
  discounted sale is tracked **separately** from full-price sales.
- Full-price payment → validates the price. Discounted payment → validates demand *at the
  discount*. Refusal-then-discount → proves price sensitivity, not $199 WTP.

## Run these two NOW (in parallel, ~2 weeks)

### A. Founding annual-prepay offer (highest signal)
Email the 144 with a real Stripe **annual** checkout link. Buyer must complete checkout —
no "reply yes."
- Pro annual **$2,388/yr** (full price) — fallback $1,791/yr (FOUNDING25), tracked separately.
- Business annual **$5,988/yr** — fallback $4,491/yr.
- Agency annual **$17,988/yr** — fallback $13,491/yr.
- (The dashboard billing tab now has a monthly/annual toggle, and annual checkout is live.)

### B. 8–10 founder-led price interviews
Only with real buyers (WA wholesalers/flippers/agents actively buying distressed leads).
Don't ask "would you pay?" Ask:
1. What do you use today for probate / pre-foreclosure / tax-delinquent leads?
2. How many deals came from that source in the last 12 months?
3. What do you pay now?
4. If BridgeLeads delivered daily WA county lists + skip-trace, would you buy today at
   **$199/mo or $1,999/yr**? — then **send the checkout link on the call.**

Skip for now: Van Westendorp survey (weak pre-revenue), fake-door A/B (need more traffic
than 144 warm accounts), bare LOIs (politeness theater without a card on file).

## The one metric + the smallest instrumentation
**Pricing intent → paid checkout completion**, split full-price vs FOUNDING25:
```
pricing_viewed → checkout_started → checkout_completed   (segment by coupon)
```
- `checkout_completed` (amount, interval, coupon, monthly-vs-annual): **already visible in
  the Stripe Dashboard** — no code needed for the 2-week test.
- `checkout_started` (intent that didn't convert + the in-app source) is the only gap Stripe
  doesn't show. With a funnel this small, a manual outreach tracker (who got the email → who
  clicked → who paid) covers it. *Optional follow-up:* a one-line structured log in
  `POST /billing/checkout` (no migration) or a small `conversion_events` table if we later
  need it at scale. **Don't** add Segment/Amplitude for this.

## Pitfalls (how not to fool ourselves)
- **Founder-friend bias** — ≥50% of payers must be outside your close network to count.
- **Discount false positives** — FOUNDING25 = "I like a deal," not "$199 is worth it."
- **Whale overfitting** — 1–2 big annual prepays don't prove repeatable SMB pricing.
- **Concession creep** — no bespoke counties / manual exports / private pricing during the
  test unless logged as a separate (non-validating) bucket.
- **Vanity metrics** — pricing-page clicks are not WTP; only checkout completion is.

## Go / No-Go decision rule (after 2 weeks)
**GO at $199/mo if ALL of:**
- ≥ **3** customers prepay annual at **full** Pro price ($2,388/yr), **or** ≥ **5** at no
  lower than $1,791/yr; **and**
- ≥ **50%** of payers are not founder friends / close network; **and**
- **no** custom manual service was required to close them.

**NO-GO / revise pricing if:**
- Fewer than 3 paid customers; **or**
- Most buyers convert only with FOUNDING25; **or**
- Interviews show buyers anchor to ~$99/mo tools and don't value daily freshness; **or**
- Buyers want pay-per-lead / county-specific pricing instead of a subscription.

If NO-GO: build only enough to test the next packaging hypothesis (likely county-specific,
pay-per-record, or a lower entry plan with usage expansion). The entitlement validator
(`src/api/entitlements.py`, feature-flagged off) is the lever to make a county/record-type
packaging real once a packaging hypothesis is validated.
