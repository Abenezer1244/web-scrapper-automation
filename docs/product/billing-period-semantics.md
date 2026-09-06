# Billing period semantics: why quota resets on the calendar month

**Status:** accepted, with known gaps listed below.
**Decided:** 2026-09-06, after the quota-counter incident (PRs #223–#226).
**Reviewers:** Claude + Codex (independent).

## The policy

Record quota resets on the **calendar month, in UTC**. A daily beat task
(`reset_monthly_usage`, 00:05 UTC) rolls over any user whose
`records_period_start` names an earlier month, and the billing path rolls the
period forward in the same statement that charges — so a still-stale period
proves nothing was billed in the current period.

This is deliberately **independent of the Stripe billing anniversary**.
Subscriptions renew on the signup date; quota does not.

## What that costs us, quantified

A customer whose first billing period crosses a calendar reset can consume up
to **2× their plan quota before the second charge**. Over N paid months the
maximum lifetime grant is roughly `(N + 1) × L`, so the over-grant amortizes to
`1 + 1/N` — worst for first-cycle churn, negligible for long-tenured accounts.

| Plan | Worst-case extra on one payment |
|---|---|
| Pro (1,000/mo) | +1,000 records |
| Business (5,000/mo) | +5,000 records |
| Agency (unlimited) | not applicable |
| Starter (free) | no revenue leakage, but inconsistent semantics |
| 7-day trial crossing a month boundary | +1,000 free records |

The grant is systematically **≥** what was paid for and never less, so the
exposure is over-delivery, not under-delivery — with one exception, below.

**Cancel-and-resubscribe is not an abuse vector.** Neither
`customer.subscription.deleted` nor the checkout handler resets
`records_used`, so cycling a subscription inside one calendar month does not
mint a fresh bucket. The reset cadence bounds it. (New-account abuse is a
separate question this note does not address.)

## Known gaps — none currently fixed

Surfaced by the Codex review of this decision. Recorded so they are chosen,
not merely inherited.

1. **Trial → paid does not reset usage.** This is the one gap that harms the
   *user*: someone who consumes their trial quota converts to paid and receives
   nothing until the calendar 1st. They have paid for a month and may get days
   of it. **This is the most defensible thing on this list to fix first.**
2. **Upgrade mid-month** raises `records_limit` but keeps `records_used`, so the
   user gets the larger plan's remaining calendar capacity immediately and a
   full fresh bucket at the next reset.
3. **Downgrade mid-month** lowers `records_limit` but keeps `records_used`, so a
   user can sit over cap until the next reset.
4. **`past_due`** does not suspend quota. Calendar resets continue until a
   `customer.subscription.deleted` event downgrades the plan.
5. **Annual prices** advertise monthly records, so resetting on the Stripe
   renewal would be plainly wrong for them — any anniversary-based design has to
   treat the entitlement period as distinct from the invoice period.

## Why we did not switch to anniversary periods now

Doing it properly is not "reset on `current_period_end`". It needs an
app-level quota window (`quota_period_start` / `quota_period_end`) that is
populated from Stripe for monthly prices, derived into monthly sub-periods for
annual prices, and app-managed for everyone without a subscription at all —
trials, Starter, and admin-granted plans. It also needs lazy rollover on the
usage path so a late webhook cannot strand a renewed payer at cap, a
reconciliation job for a webhook that never arrives, and a backfill that
advances existing users without resetting anyone's counter.

That is a billing-semantics change touching every paying customer. It deserves
its own design and review cycle rather than riding along at the end of an
incident fix.

## What we did instead

`GET /billing/usage` now reports `period_start`, `next_reset_at`, and
`period_basis: "calendar_month_utc"`. Calendar-month quota is defensible **only
if it is explicit**, and it was not: the product copy says "1,000
records/month" while the UI showed a bare number that silently changed
overnight. The frontend should surface the reset date, and the pricing copy
should say quotas reset on the first of each calendar month (UTC).

## If we revisit

Fix gap 1 first — it is the only one that takes something away from a customer
who has paid. Gaps 2–4 all over-deliver, which is the safer direction to leave
standing while the larger design is worked out.
