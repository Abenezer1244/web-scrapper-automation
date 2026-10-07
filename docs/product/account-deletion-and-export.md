# Account deletion and data export: proposed design

**Status:** owner-approved engineering defaults, 2026-10-06 (§4). Nothing here is built.
The §4 decisions came from engineering research (three independent passes), not legal
advice; the open items in §4.3 still go to counsel (see `docs/legal/COUNSEL-BRIEF-retention-2026-09-17.md`).
Until it ships, Settings > Account tells users to email support for an export or
to close their account. That is a real, supported path; there is no fake button.

## 1. Why not just `DELETE FROM users`

Verified in `src/db/models.py` on 2026-10-05:

- Every foreign key to `users.id` is `ON DELETE CASCADE` (21 of them: scraper
  configs, batches and runs, jobs, results, delivered_records, notifications,
  skip-trace queues/rows/cache links, **skip_trace_meter_events**, referral events,
  dialer deliveries, contact-lookup actions, password history, MFA codes, and the
  migration-111 tables). One `DELETE` would erase billing evidence
  (`skip_trace_meter_events`, contact-lookup ledgers) that we may need for a
  chargeback, a Stripe dispute or tax records.
- `users.referred_by_user_id` is `SET NULL`; `audit_events.user_id` has no FK on
  purpose (the trail survives the user).
- Delivered export files live in Cloudflare R2 and are untouched by any cascade.
- The Stripe customer and any live subscription are untouched by any cascade.
- Neither runtime DB role holds `DELETE` on `users` (the API role has no DELETE
  anywhere but two staging tables). Hard deletes run only through the
  `DATABASE_URL_MIGRATE` path.

## 2. Proposed flow

1. **Request** (Settings > Account > Your data > Delete account): signed-in
   session + current password + 2FA code when enabled. The dialog spells out what
   happens to: profile, scraper configurations, schedules, leads/results, lists,
   API keys, integrations (dialer), billing/subscription, stored exports, batch
   history. The user types their email to confirm.
2. **Immediately:** cancel the Stripe subscription at period end (no new charges),
   pause every schedule, clear the API key, sign out every session
   (`users.revoked_at`), set `users.deletion_requested_at`. The account stops
   doing anything but still exists.
3. **Grace period (decided: 30 days, see §4):** signing in shows "This account is
   scheduled for deletion on <date>" with a Restore button. Restoring clears the
   flag; schedules stay paused for the user to resume.
4. **Purge (beat task, worker role + migrate role for the deletes):**
   - delete R2 objects under the user's export prefix;
   - delete tenant data that carries personal information about third parties
     (results, skip-trace links, lists, deliveries, contact lookups);
   - **retain** the categories in §4.1, with the user's profile PII removed: `users`
     row tombstoned (email/name/phone/avatar nulled, `email_hmac` replaced so the
     address can register again, `is_active=false`, `deleted_at` set). Retained rows
     are *pseudonymised* (linkable via Stripe), never described as "deidentified";
   - `delivered_records` (who received which parcel) is **kept**, not purged: it is
     the only way to pass a homeowner's later deletion request on to the customers
     who received that lead;
   - Stripe: once no invoice or refund is open, delete the Stripe Customer (§4.4).
   - Each step idempotent and recorded on a `account_deletions` row (outbox
     pattern, like `pending_email_changes`), so a crash resumes, never half-runs.
5. **Confirmation email** to the address the account had, after the purge.

## 3. Export

`POST /auth/export` (session + password) enqueues a worker job that writes one ZIP
to R2: profile JSON, scraper configs, schedules, batch history, and every lead CSV
already deliverable to the user, using the existing `DataExporter` (CSV-injection
sanitised). The user gets an expiring signed link by email and in-app. One export
per 24 h per account. Same RLS and `user_id` filters as every other read.

Decided 2026-10-07 (owner, P4): an account scheduled for deletion cannot download any
export, including one made before the request (the request already revokes every link).
The delete dialog says "download your data first"; a user who forgot restores, downloads,
and asks again. Deletion supersedes an export still being built. The link lasts 7 days;
`account_exports` rows (ids, timestamps, status) are kept 24 months as the request log.
Build plan and review log: `tasks/todo-account-deletion.md` (P4).

## 4. Decisions (owner, 2026-10-06)

Reconciled from three independent research passes (Claude, Perplexity, ChatGPT).
Sources: RCW 82.32.070 + WAC 458-20-254 (WA tax records, 5 years); IRS record
periods (3/6/7 years); Cal. Civ. Code 1798.105(d) and 11 CCR 7022/7101;
Stripe docs ("Delete a customer", "Redact personal data", "Handling customer
deletion requests").

### 4.1 What survives the purge, and for how long

| Record | Kept for | Why |
|---|---|---|
| Billing: Stripe ids, `skip_trace_meter_events`, contact-lookup ledgers, invoices | 7 years | WA requires 5; 7 also covers the IRS 6- and 7-year cases. Keeps amount, date, product and location fields; profile details removed |
| Audit events: deletion, MFA, password, export, admin changes | 24 months | Security / fraud exception; also the dispute evidence (terms, cancellation, use) |
| Audit events: sign-ins and other routine events | 12 months | No legal minimum found; keep less |
| `delivered_records` (customer -> parcel) | 24 months after last delivery | Needed to forward a homeowner deletion request to recipients |
| Everything else (profile, settings, schedules, results, skip-trace data, R2 exports, sessions, dialer config, avatar) | Deleted at purge | No retention purpose |

No separate "dispute evidence" store: the 7-year billing records plus the 24-month
high-value audit events already hold it. The expiry jobs for these periods are a
later phase; the first purge only has to stop deleting them.

### 4.2 Grace period and billing

- 30 days, user can undo by signing in and pressing Restore. The purge date is
  the earlier of day 30 and any legal deadline (CCPA: 45 days from the request,
  not from verification). In-app "Delete account" counts as the deletion request.
- Billing: the subscription is set to cancel at period end, no refund; the dialog
  says so. Restoring before period end un-cancels it; after period end the user
  subscribes again.

### 4.3 Downloaded files, and open items for counsel

- Policy must say plainly that deletion cannot erase files already downloaded,
  and that the customer remains responsible for those copies (Terms §5(c)).
  Draft wording for counsel: ChatGPT research pass, "Previously exported lead data".
- Homeowner removal requests are a **separate workflow** (suppression list so a
  removed homeowner does not come back on the next scrape). Next project after
  this one. The published privacy contact address (`bridgeleads.com`) does not
  receive mail and must be fixed first.
- For counsel: California coverage and data-broker status field by field; WA
  sales-tax classification after the Oct 2025 changes (decides which location
  fields billing must keep); an FCRA prohibited-use clause; how to reach a former
  customer for a downstream notice after their email is erased.

### 4.4 Stripe

Keep the Customer through the grace period and final invoice (deleting it is
irreversible and cancels subscriptions immediately). At purge, once nothing is
open, delete the Customer. Finalized invoices keep their own copy of the
customer's details, so the tax record survives. Never use Stripe redaction: it
cannot redact invoices, and a redacted payment cannot be refunded and loses any
dispute automatically.

## 5. Size

Migration (deletion-request + tombstone columns, `account_deletions` outbox),
two routes, one beat task, one worker export job, Settings UI with the typed
confirmation. Roughly 3 to 4 PRs. Tests must cover: cross-tenant isolation of the
purge, idempotent resume, Stripe failure retry, restore during grace, and that
retained billing rows no longer carry PII.
