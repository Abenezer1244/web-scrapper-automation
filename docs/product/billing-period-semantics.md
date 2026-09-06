# Billing period semantics

**Status:** **SUPERSEDED** by entitlement periods (migration 088, PR #231).
The calendar-month policy below is no longer the design. It is kept as the
historical record of why it was chosen and what it cost.

> ⏭️ **Deployment state:** the replacement is **merged-ready but NOT yet
> deployed** as of 2026-09-06. Until PR #231 ships, production still behaves
> as the historical section describes. Flip this note when the deploy lands.

**Superseded:** 2026-09-06 — the same day it was accepted. That is not churn:
the note below closes with *"fix gap 1 first — it is the only one that takes
something away from a customer who has paid,"* and that is exactly what
happened. The design it asked for got its own cycle.

**Replaced by:** `tasks/todo-entitlement-periods.md` (the nine approved
policies, the design, the 5-round Codex gate, the §14 security review) and
`docs/HANDOFF-entitlement-periods-DEPLOY-2026-09-06.md` (the deploy sequence).

---

## The policy now

Record quota is metered over an **entitlement window** —
`[quota_period_start, quota_period_end)` — on a monthly grid anchored at
`users.quota_anchor_at`, always exactly one month long however Stripe invoices.
Quota resets on **each customer's own monthly anniversary**, not the 1st.

The invariant the whole surface rests on: **plan and status changes never move
the anchor or the window.** The anchor moves on exactly three events — first
trial→paid conversion, resubscribe after a genuine lapse, and explicit admin
action. That single rule is what makes upgrade-farming and
cancel/resubscribe-farming worthless.

Rollover is **lazy**: it happens inside the statement that next charges the
user, with an hourly `reconcile_quota_periods` beat for people who never
transact. Payment is never the trigger — a webhook-driven reset would strand a
renewed payer at cap behind a late delivery and hand out a second bucket on a
replay.

`GET /billing/usage` now reports `period_basis: "entitlement_month_utc"` (was
`calendar_month_utc`), plus `pending_plan`, `pending_records_limit`,
`payment_state` and `entitlement_ends_at`.

**Skip-trace quota is deliberately still calendar-metered.** It keeps its own
column and the surviving half of the legacy beat. Out of scope, on purpose.

## Disposition of the five known gaps

The historical note listed five gaps and fixed none of them. Where each landed:

| # | Historical gap | Now |
|---|---|---|
| 1 | **Trial → paid does not reset usage** — the one gap that harmed a paying user | **FIXED.** P1: the first paid conversion re-anchors to `billing_cycle_anchor` and zeroes `records_used`, idempotent via a CAS on `first_paid_at IS NULL` so a replay cannot zero twice. Re-trialling grants nothing (`trial_consumed_at` is permanent). |
| 2 | **Upgrade mid-month** raises the limit, keeps `records_used` | **KEPT — now deliberate,** not inherited. P4: immediate, same window, same counter, higher limit. Resetting here is exactly what would make upgrade-farming pay. |
| 3 | **Downgrade mid-month** can leave a user over cap | **FIXED.** P5: deferred to the next entitlement boundary via `pending_plan` / `pending_records_limit`. `3000/5000` never becomes `3000/1000`. Costs up to one cycle of over-delivery, which is the safe direction. |
| 4 | **`past_due` does not suspend quota** | **FIXED.** P7: a 7-day grace (`BILLING_PAST_DUE_GRACE_DAYS`), then freeze — new billable work refused and **the window stops advancing**, so a delinquent account cannot accrue buckets. No data deleted. Recovery lands one bucket, not the backlog. |
| 5 | **Annual prices advertise monthly records** | **FIXED.** P3: the entitlement window is always one month regardless of invoice interval. `_PRICE_TO_PLAN` carries `interval`, so monthly↔annual is knowable and is a no-op for quota. |

## What the quantified cost becomes

The table below priced the calendar policy's worst case at **+1,000 records on
one Pro payment** and **+5,000 on Business**, plus **+1,000 free records** for a
7-day trial straddling the 1st. All three are eliminated: the first cycle is a
window anchored at conversion, and the trial is its own window
`[signup, trial_ends_at)` rather than a calendar month.

## Two latent bugs the old policy was hiding

Neither was noticed while every window started on the 1st, because
month-equality was *accidentally* exact:

- `_reservation_is_current` compared calendar **months**. With a 20th anchor, a
  job reserving on the 19th and settling on the 21st reads as "same period", so
  settlement nets `billable − reserved = 0` against a counter the rollover
  already zeroed — **the delivered records are charged to nobody.**
- `release_quota_reservation` is the mirror image: refunding a grant into a
  window that never held it, destroying current-window usage.

`jobs.quota_period_start` now records which window a grant was charged to, and
both sites compare windows instead of months. They had to be fixed in the same
change that introduced non-calendar windows, not after.

---

# Historical record — why quota reset on the calendar month

*Everything below was accurate policy until migration 088. Preserved unedited
apart from this heading.*

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

*(That cycle is the one that produced migration 088. Every requirement listed
in this paragraph is in the shipped design.)*

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
