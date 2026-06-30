# expire_trials: gate on Stripe entitlement, not stripe_customer_id

## Bug
The hourly `expire_trials` task downgraded expired trials Pro→Starter UNLESS
`stripe_customer_id IS NULL`. But a `stripe_customer_id` is created when a user
merely OPENS Stripe checkout — not when they pay. So a trial user who opened
checkout but never paid kept Pro forever. (Confirmed in prod: mikitsegaye29 — has
a customer id, no subscription, expired trial, stuck on Pro.)

## Codex consult (reconciled — Codex's pick adopted)
- Don't use `stripe_customer_id` as an entitlement signal ("touched Stripe" ≠ "paid").
- Add durable `stripe_subscription_id` + `subscription_status`; gate on an entitled
  status. Protect `active`/`trialing`/`past_due` (past_due avoids downgrading mid
  dunning). Final entitlement loss comes from `subscription.deleted`.
- Deploy order: migration → webhook population → backfill → THEN gate. Safe to ship
  together NOW because prod has no active paid subscriber (admin trial_ends_at=NULL
  so untouched; the one expired-trial user correctly downgrades). No backfill needed.

## Changes
- [x] mig 077: add `stripe_subscription_id` + `subscription_status` (nullable).
- [x] models.py: 2 columns on User.
- [x] billing.py webhooks:
      - checkout.session.completed: set sub id + authoritative `subscription.status`
        (already retrieved) + clear `trial_ends_at` (converted ≠ on trial).
      - subscription.updated: set sub id + status; clear trial if active/trialing.
      - subscription.deleted: clear sub id, status="canceled".
- [x] scheduler_helpers/billing.py: gate = trial_ends_at<now AND plan!=starter AND
      (sub_id IS NULL OR status IS NULL OR status NOT IN active/trialing/past_due).
- [x] tests/test_expire_trials.py: bug case + all entitled/non-entitled statuses +
      active-trial + no-trial(admin) — 8 cases, real Postgres, no mocks.
- [x] py_compile clean; single alembic head 077. (No local PG → CI validates suite.)
- [x] Codex review #1 → **[P1]** the gate could downgrade a LEGACY payer whose new
      fields are still NULL (paid before mig 077). FIXED: gate now downgrades only
      with POSITIVE non-payment evidence — never an ambiguous (customer id + NULL
      status) row. Safe regardless of deploy order.
- [x] scripts/backfill_subscription_status.py: resolve ambiguous rows from Stripe
      (entitled → store + clear trial; else → set status so the gate can downgrade).
- [x] tests updated: ambiguous row is PROTECTED; canceled-status row downgrades.
- [ ] Codex review #2 (confirm P1 closed).

## Failure modes (Codex) — addressed
- Wrongly downgraded mid-cycle: protected statuses + webhook keeps state current;
  expire logs each downgrade. past_due covered.
- Keeps Pro free: status cleared on subscription.deleted; gate treats NULL/unknown
  status as not-entitled. Follow-up: periodic Stripe reconciliation job (out of scope).

## Out of scope (follow-ups)
- Backfill existing paying users from Stripe (none exist now).
- Periodic Stripe↔DB reconciliation job for drift.
- Deploy: run migration 077 BEFORE the worker picks up the new gate (additive
  nullable cols → safe; gate already tolerates NULL).
