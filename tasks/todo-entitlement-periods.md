# Entitlement periods — design (AWAITING APPROVAL, nothing implemented)

Branch: `feat/entitlement-periods`
Worktree: `C:/Users/Windows/bridgeleads-worktrees/entitlement`
Base: `a009f15` == `origin/main` HEAD (verified — nothing has landed since the handoff).

Goal: record quota stops being governed by the 1st of the calendar month.

---

## 0. What I verified in the code (not taken on trust)

| Handoff claim | Verdict | Evidence |
|---|---|---|
| Only 4 webhook events handled | TRUE | `src/api/routes/billing.py:716-726` |
| `invoice.payment_succeeded` NOT handled | TRUE | same block — no branch for it |
| `_PRICE_TO_PLAN` collapses monthly+annual | TRUE | `billing.py:306` — `dict[str, tuple[str, int]]`, both price ids map to the same tuple |
| `subscription.deleted` downgrades instantly | TRUE | `billing.py:_handle_subscription_deleted` |
| `payment_failed` only emails | TRUE | `billing.py:_handle_payment_failed` |
| `User` has no Stripe period fields | TRUE | `src/db/models.py` User — only `stripe_customer_id`, `stripe_subscription_id`, `subscription_status`, `trial_ends_at` |
| No webhook writes `records_used` | TRUE | grep: `records_used` appears in `billing.py` only via the `/usage` reader |
| Lock order jobs -> users | TRUE | `tasks.py:1352-1360`, `tasks_helpers/status.py:181+` |

### The period rule is currently expressed in SEVEN places, not four

Codex flagged four. There are seven; two of the three it missed are correctness-critical.

1. `src/api/quota.py::effective_records_used` — the API/enforcement expression.
2. `src/workers/tasks.py:1395-1413` — RESERVE (`CASE WHEN records_period_start < date_trunc('month', ...)`).
3. `src/workers/tasks.py:1796-1808` — SETTLE.
4. `src/workers/tasks.py:1779-1786` — `_reservation_is_current` (month equality, reserved_at vs billed_at).
5. **`src/workers/tasks_helpers/status.py:218-228` — `release_quota_reservation`** (month equality, `users.records_period_start` vs `jobs.reserved_at`). *Codex missed this as a drift site.*
6. `src/workers/scheduler_helpers/billing.py:71-102` — the beat (adopt-NULL + roll-stale).
7. **`src/api/routes/billing.py:478-483` — the `/usage` `next_reset_at`** inlines its own month arithmetic. *Codex missed this.*

Plus `scripts/cleanup_watchdog_billed_dups.py` (`period_current = effective_billed_at >= records_period_start`).

### The reservation-boundary bug is worse than "netting the wrong window"

`_reservation_is_current` compares CALENDAR MONTHS. With a Sep-20 anchor, a job that
reserves on Sep 19 and settles on Sep 21 is "same month" -> judged current -> settlement
nets `billable - reserved`. But the Sep-20 rollover already zeroed the counter, so the
reserved amount was never in it. Delta 0 means those delivered records are **charged to
nobody**. Silent free quota, once per boundary-straddling job.

The mirror image lives in `release_quota_reservation` (site 5): after an anchor-day
rollover it would refund a grant into a window that never held it, destroying
current-period usage — the exact class of bug PR #223 exists to fix.

Both are unreachable today (every window starts on the 1st, so month-equality happens to
be exact). Both go live the moment any anchor is not day 1. **They must be fixed in the
same change that introduces non-calendar windows, not after.**

### Other findings of my own

- `_expire_trials_impl` changes `plan` + `records_limit` but never touches the period or
  the counter. Under windows, trial expiry must also close the trial window.
- Registration (`auth_helpers/registration.py:163-176`) gives every new user
  `plan="pro"`, `records_limit=1000`, `trial_ends_at=now+7d`, and a CALENDAR window. So a
  7-day trial straddling the 1st gets 2,000 free records today. The window model fixes
  this as a side effect — a real (favourable) product change, flagged below.
- No webhook handler compares Stripe event timestamps, so an out-of-order
  `subscription.updated` can overwrite newer state. Codex is right; confirmed.
- `stripe==11.4.0` is pinned, where `StripeObject` still behaves as a dict, so
  `subscription.get("billing_cycle_anchor")` is safe. (Do not bump — see the v15 landmine.)

---

## 1. Data model

`users` — new columns:

| Column | Type | Purpose | Written by | Read by | Backfill | Derivable? |
|---|---|---|---|---|---|---|
| `quota_anchor_at` | timestamptz NOT NULL | Immutable origin of the monthly grid. Every boundary is computed by adding whole months to THIS value, never to the previous (possibly clamped) boundary. | Only: trial->paid conversion, resubscribe-after-lapse, admin action, migration backfill. | The rollover clause at every site. | `= records_period_start` (so every user starts on a day-1 grid = today's behaviour exactly). | No — Stripe `billing_cycle_anchor` is not stored, and a Stripe call cannot happen in a hot path. |
| `quota_period_start` | timestamptz NOT NULL | Start of the CURRENT window. | The atomic rollover clause in reserve / settle / release / reconcile. | Enforcement gates, `/usage`, reservation comparison. | `= records_period_start`. | No — transitional windows after an anchor change are off-grid. |
| `quota_period_end` | timestamptz NOT NULL | End of the current window. The single staleness test is `quota_period_end <= now`. | same | same | `= records_period_start + 1 month`. | No (same reason). |
| `entitlement_ends_at` | timestamptz NULL | When paid access stops (cancel-at-period-end). NULL = open-ended. | `subscription.updated` / `.deleted`. | Rollover (refuses to open a window starting at/after it), enforcement. | NULL. | No — comes from Stripe. |
| `entitlement_grace_ends_at` | timestamptz NULL | End of the `past_due` dunning grace. | `invoice.payment_failed` (set if NULL); cleared on recovery. | Enforcement + rollover freeze. | NULL. | No. |
| `trial_consumed_at` | timestamptz NULL | The app trial has been used. Permanent. | Trial expiry and trial->paid conversion. | Trial-grant eligibility. | `= trial_ends_at` where non-NULL, else `created_at` for anyone already past a trial. | No — `trial_ends_at` is CLEARED on conversion, so it cannot answer "did they ever trial". |
| `first_paid_at` | timestamptz NULL | Idempotency key for the one-time trial->paid quota reset. | Conversion handler, CAS'd on `IS NULL`. | Conversion handler. | `= created_at` for users whose `subscription_status IN ('active','past_due')` at migration time (already converted). | No. |
| `paid_entitlement_ended_at` | timestamptz NULL | When the last paid entitlement genuinely lapsed. Gates "resubscribe mints a fresh window". | `subscription.deleted`. | Resubscribe handler (which clears it). | NULL. | No. |
| `pending_plan` / `pending_records_limit` | varchar / int NULL | A DOWNGRADE queued for the next boundary. | `subscription.updated`. | Rollover (applies + clears). | NULL. | No. |

`jobs` — new column:

| Column | Type | Purpose | Written by | Read by | Backfill | Derivable? |
|---|---|---|---|---|---|---|
| `quota_period_start` | timestamptz NULL | The window this job's reservation was charged to. NULL = never reserved / pre-migration. | The reserve statement, in the SAME statement that stamps `reserved_at`. | Settlement (`_reservation_is_current`), release, sweep. | NULL (in-flight jobs keep `reserved_count`-based behaviour, exactly as at the #224 deploy). | No — this IS the fix for the boundary bug. |

**Rejected: `quota_period_source`** ('calendar' / 'subscription' / 'trial'). Codex is right
that it invites drift — `plan=pro, status=active, source=calendar` is representable and
meaningless. It is derivable for display from `plan` + `subscription_status` +
`trial_ends_at`. Not stored.

**Also rejected: storing only a `quota_anchor_day` smallint.** The instant matters (a
conversion at 14:37 UTC should not silently move the customer's reset to midnight), and a
stored day-of-month re-introduces the "did we clamp 31 to 28 permanently?" bug that
storing the original instant makes structurally impossible.

`records_period_start` / `skip_trace_period_start` are KEPT, and `records_period_start` is
written in lockstep with `quota_period_start` for one release so the legacy task, `/usage`
and `scripts/cleanup_watchdog_billed_dups.py` keep working. Dropped later.

**Skip-trace quota is explicitly OUT OF SCOPE** — it stays on the calendar month, keyed on
its own column, handled by the surviving half of the legacy beat. Follow-up.

---

## 2. Month arithmetic

Boundaries are `quota_anchor_at + k months` for integer `k >= 0`, always added to the
ORIGINAL anchor. Postgres clamps correctly and does NOT compound the clamp: Jan 31 + 1
month = Feb 28, Jan 31 + 2 months = Mar 31.

Anchor Jan 31 -> Jan 31, Feb 28 (Feb 29 in a leap year), Mar 31, Apr 30, May 31 — the
required progression, satisfied by construction.

**Rollover rule** (one definition, used by every site):

    s1 = old quota_period_end
    e1 = the grid boundary CLOSEST to (s1 + 1 month); ties -> the later one
    if now < e1:  new window = [s1, e1)
    else:         new window = the grid cell containing now
    records_used = 0        (exactly once, however many cells were skipped)

In steady state `s1` IS a grid boundary, so `s1 + 1 month` IS the next boundary and the
rule is exact — no special case. The "closest boundary" clause only fires on the first
rollover after an anchor change, and bounds that one-time transitional window to within
~15 days of a month in either direction.

Everything is UTC `timestamptz`. Browser timezone never enters. All arithmetic is
server-side; the Python helper exists for `/usage` and tests and is cross-checked against
the SQL over a generated date matrix (Jan 31 anchors, Feb 29, multi-month gaps, DST dates).

---

## 3. THE NINE POLICIES — these need approval

**P1. Trial -> paid.** On the first transition to a paid-entitled subscription
(`checkout.session.completed`, or `subscription.updated` reaching active/trialing) for a
user with `first_paid_at IS NULL`: set `quota_anchor_at = subscription.billing_cycle_anchor`
(fallback `current_period_start`, then now); set the window to the grid cell containing
now; **`records_used = 0`**; set plan + limit; `trial_ends_at = NULL`; stamp
`trial_consumed_at` and `first_paid_at`. Idempotent via a CAS on `first_paid_at IS NULL`,
so a replay — or a second event describing the same conversion — cannot zero the counter
twice. Anti-farming is by ELIGIBILITY, not window logic: `trial_consumed_at` is permanent,
so cancel-and-re-trial grants nothing.
*Side effect (product change — flag it):* the trial window becomes the 7-day trial rather
than the calendar month, so a trial straddling the 1st stops handing out 2,000 free records.

**P2. Monthly renewal.** Quota follows the entitlement anniversary, NOT the 1st. No webhook
is required: the window advances LAZILY the first time any authoritative quota operation
sees `now >= quota_period_end`, and a reconciliation beat does the same for users who never
transact. `invoice.payment_succeeded` IS added, but only to refresh `subscription_status` /
`entitlement_ends_at` and re-affirm the anchor — it never zeroes the counter and never
advances a window. (A payment-triggered reset would strand a renewed payer behind a late
webhook and would double-grant on a replay.)

**P3. Annual.** Identical to monthly — the entitlement window is ALWAYS one month. The
anchor is `billing_cycle_anchor` (the annual anniversary instant); its day-of-month drives
the monthly grid. `_PRICE_TO_PLAN` becomes `{plan, records_limit, interval}` so monthly vs
annual is knowable; a monthly<->annual switch is the same subscription and therefore a
NO-OP for quota.

**P4. Upgrade (limit increases).** Immediate. Keep the window, KEEP `records_used`, raise
`records_limit` only. `600/1000` -> `600/5000`. No new window, no reset — that is what
makes upgrade-farming worthless.

**P5. Downgrade (limit decreases).** DEFERRED to the next entitlement boundary.
`subscription.updated` records `pending_plan` / `pending_records_limit`; the rollover
applies and clears them. Until then the customer keeps the limit they already paid for, so
`3000/5000` never becomes `3000/1000`. Cost: up to one cycle of over-delivery on a prorated
downgrade — bounded, and the safe direction.
Exceptions applied IMMEDIATELY (not customer-paid states): trial expiry, and admin action.
Entitlement (county / record-type) reconciliation defers along with the plan.

**P6. Cancellation — four distinct cases.**
- a. *cancel_at_period_end* (`subscription.updated`, `cancel_at_period_end=true`): nothing
  changes now. Store `entitlement_ends_at`. Quota keeps rolling monthly UP TO it; the
  rollover refuses to open a window starting at or after `entitlement_ends_at`.
- b. *term actually ended* (`subscription.deleted`): plan -> starter, limit -> 50, status
  canceled, `stripe_subscription_id` cleared, `paid_entitlement_ended_at = now`.
  **The window and the anchor do NOT move and `records_used` is NOT reset** — they keep the
  counter they earned and regain quota at their own next boundary.
- c. *immediate cancellation*: same as (b); Stripe sends the same `deleted` event.
- d. *refund / administrative termination*: NOT a webhook path. An explicit, audit-logged
  admin action that may set plan, limit and window directly. Out of automatic scope.

**P7. `past_due`.** Serve normally for a grace period —
`entitlement_grace_ends_at = <first payment_failed> + GRACE_DAYS` (proposed **7 days**, via
`settings.BILLING_PAST_DUE_GRACE_DAYS`). After it: FREEZE — new billable work is refused
(402) and **the window stops advancing**, so a delinquent account cannot accrue fresh
buckets. Existing data, results and exports stay available; nothing is deleted. Recovery
(`payment_succeeded`, or status -> active) clears the grace and lazily advances to the cell
containing now — ONE bucket, no accumulation for the frozen months.

**P8. `unpaid` / failed payment.** `unpaid` (Stripe has given up dunning): freeze
immediately, no grace — no new billable scraping work, window does not advance. Plan and
limit are NOT changed and **no customer data is deleted**. `incomplete`,
`incomplete_expired` and `paused` are treated the same. Starter / free / admin-granted
accounts have no subscription and are never frozen. Recovery restores.

**P9. Resubscribe.**
- While the previous entitlement is still live (un-cancel, or a new sub before
  `entitlement_ends_at`): **no anchor change, no reset.** They resume the existing window
  and counter. Cancel/resubscribe farming mints nothing.
- After the entitlement genuinely lapsed (`paid_entitlement_ended_at IS NOT NULL` AND
  `now > entitlement_ends_at`): a new paid subscription re-anchors to the new
  `billing_cycle_anchor` and starts a fresh window with `records_used = 0`. Guarded on
  `paid_entitlement_ended_at`, cleared by the same statement, so a replayed event cannot
  re-mint.
- The app trial is never re-granted (`trial_consumed_at`).

### The invariant that ties P4-P9 together

**Plan and status changes never move the anchor or the window.** The anchor moves on
exactly three events: first trial->paid conversion, resubscribe-after-genuine-lapse, and
explicit admin action. Upgrade, downgrade, cancel, past_due, recovery and monthly<->annual
change only `plan` / `records_limit` / status fields. That single invariant is what makes
the whole surface un-farmable.

---

## 4. Reservation crossing a window boundary

`jobs.quota_period_start` is stamped in the same statement as `reserved_at`.

`_reservation_is_current` becomes, evaluated under the user row lock at settlement:

    jobs.quota_period_start = users.quota_period_start
    AND users.quota_period_end > billed_at

i.e. "the window this grant was charged to is still the live one". Not current ->
`reserved = 0` and the FULL `billable_count` is charged to the current window.

The handoff's example: reserve 200 at 11:59 in W1; W1 ends at 12:00; settle at 12:05.
`jobs.quota_period_start` (W1) != `users.quota_period_start` (W2) -> the 200 charged to W1
went away with W1's zeroing -> **W2 is charged the full delivered count**. The leads are
delivered now, so the live window owns them. This is exactly today's documented intent; it
is only the COMPARISON that was wrong.

`release_quota_reservation` and `sweep_stranded_quota_reservations` get the same equality
guard in place of their month comparison: a stale-window reservation refunds NOTHING (there
is nothing to refund) but still clears `reserved_at` / `reserved_count` so it cannot be
handled twice.

Lock order stays **jobs -> users** at every one of these sites. No new locks.

## 5. Concurrent rollover

The rollover is never a standalone read-then-write. It is a clause inside the statement
that already holds the row:

    UPDATE users SET
      records_used       = (CASE WHEN quota_period_end <= :at THEN 0 ELSE records_used END) + :delta,
      quota_period_start = CASE WHEN quota_period_end <= :at THEN :new_start ELSE quota_period_start END,
      quota_period_end   = CASE WHEN quota_period_end <= :at THEN :new_end   ELSE quota_period_end END
    WHERE id = :uid

Two workers: the first commits, the guard `quota_period_end <= :at` is then false, so the
second's CASE arms all take the ELSE branch and it merely adds its own delta. No double
reset, no lost usage. The reservation's existing `FOR UPDATE` + LEAST/GREATEST grant
computation is untouched, so double-reserve and over-limit remain impossible. The
reconciliation beat uses the same guarded statement, so it cannot race a live reservation.
**No second quota system is introduced** — this is the existing reserve/settle machinery
with a different period predicate.

## 6. Reconciliation, not correctness

New beat `reconcile_quota_periods` (hourly). Advances any user with
`quota_period_end <= now` using the identical guarded statement, applies `pending_plan`,
and honours the P7/P8 freeze. Idempotent, tolerates missed runs (it lands on the cell
containing now and never accumulates buckets), coexists with live reservations, and is a
safety net — the lazy path on reserve/settle/enforcement is what makes quota correct.

## 7. Cutover — staged, never two grants and never none

1. **Migration 088**: add the columns. Backfill
   `quota_anchor_at = quota_period_start = records_period_start`,
   `quota_period_end = records_period_start + 1 month`. `records_used` UNTOUCHED. Every user
   is on a day-1 grid, so the new fields describe today's behaviour exactly. Deterministic
   and idempotent (re-running recomputes the same values from `records_period_start`).
2. **Read-side deploy**: `/usage` reports the new window. Verify new == old for every user.
3. **Switch the hot paths** (reserve, settle, release, sweep, the four enforcement gates) to
   the window columns + `jobs.quota_period_start`. Behaviour still identical, because every
   window is still a calendar month.
4. **Add `reconcile_quota_periods`.** While every anchor is day 1 it and the legacy
   `reset_monthly_usage` compute the same result, so running both is provably safe.
5. **Retire the records half of the legacy reset.** The task survives for skip-trace only.
   `records_period_start` keeps being written in lockstep for one release.
6. **`scripts/backfill_quota_anchors.py`** (`--dry-run` default, fail-loud): for each user
   with `stripe_subscription_id`, read `billing_cycle_anchor` and set `quota_anchor_at`
   ONLY — the live window is not touched. Each user's grid shifts at their next natural
   rollover, with the bounded transitional window from section 2. This is the only step that
   changes anyone's reset date; it runs after step 5 is verified in prod.
7. **Webhook lifecycle** (P1, P4-P9) + `invoice.payment_succeeded` + an event-ordering guard.
8. Verify production, including the live account.

At no point do both mechanisms grant (steps 1-4 agree by construction; step 5 is the single
handover) and at no point can neither roll (step 5 only lands after 3 and 4 are verified live).

**The 1007/1000 account (`zowiegirma29@gmail.com`, `01dc9396…`)** at step 1:
`quota_anchor_at = quota_period_start = 2026-09-01`, `quota_period_end = 2026-10-01`,
`records_used = 1007` untouched, `records_limit = 1000` -> still over cap, still blocked,
next reset 2026-10-01. The migration adds columns and changes no counter. After step 6 its
anchor moves to its Stripe `billing_cycle_anchor`; the transitional window is bounded by the
section-2 rule so it gets ONE bucket, not two.

## 8. Test plan

Every case from the brief, plus: the seven drift sites reduced to one; Python-vs-SQL window
agreement over a generated date matrix; reserve-before / settle-after rollover;
release-after-rollover; webhook replay and out-of-order delivery; the migration run twice.
Existing quota/reservation tests are preserved unchanged, not weakened.

## 9. Operator decisions — ANSWERED 2026-09-06

- **P5 downgrade: DEFER to the next entitlement boundary.** (Approved. Accepts up to one
  cycle of over-delivery on a prorated downgrade; never leaves a customer at 3000/1000.)
- **P7 past_due: 7-day grace**, then freeze. (Approved. `settings.BILLING_PAST_DUE_GRACE_DAYS = 7`.)
- **P6b cancellation: CARRY `records_used` over** into starter; window and anchor unchanged.
  (Approved. Keeps the "a plan change never resets the counter" invariant intact.)
- **Cutover step 6: MOVE existing subscribers to their real Stripe anniversary**, one
  bounded shift at each user's next natural rollover. (Approved.)

## 10. Open items flagged, NOT decided unilaterally

- P5 defers a downgrade by up to one cycle = up to one cycle of over-delivery.
- P7's grace length (proposed 7 days).
- The trial window narrowing from "calendar month" to "the 7-day trial" is a genuine product
  change (it removes an existing +1,000 free-record over-delivery).
- Skip-trace quota stays on calendar months (out of scope).
- Step 6 shifts every existing subscriber's reset date once, with a bounded transitional
  window. The alternative (leave existing users on day-1 forever) would not meet the brief's
  completion bar.
