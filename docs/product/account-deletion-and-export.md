# Account deletion and data export: proposed design

**Status:** proposal, 2026-10-05. Nothing here is built. Needs owner approval, and
the retention questions in §4 need counsel (see `docs/legal/COUNSEL-BRIEF-retention-2026-09-17.md`).
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
3. **Grace period (proposal: 14 days):** signing in shows "This account is
   scheduled for deletion on <date>" with a Restore button. Restoring clears the
   flag; schedules stay paused for the user to resume.
4. **Purge (beat task, worker role + migrate role for the deletes):**
   - delete R2 objects under the user's export prefix;
   - delete tenant data that carries personal information about third parties
     (results, skip-trace links, lists, deliveries, contact lookups);
   - **retain** what counsel says must survive (likely: billing ledgers, Stripe
     ids, audit events), with the user's PII removed: `users` row tombstoned
     (email/name/phone/avatar nulled, `email_hmac` replaced so the address can
     register again, `is_active=false`, `deleted_at` set);
   - Stripe customer: keep (invoices are legal records) but clear metadata and
     the email if counsel agrees.
   - Each step idempotent and recorded on a `account_deletions` row (outbox
     pattern, like `pending_email_changes`), so a crash resumes, never half-runs.
5. **Confirmation email** to the address the account had, after the purge.

## 3. Export

`POST /auth/export` (session + password) enqueues a worker job that writes one ZIP
to R2: profile JSON, scraper configs, schedules, batch history, and every lead CSV
already deliverable to the user, using the existing `DataExporter` (CSV-injection
sanitised). The user gets an expiring signed link by email and in-app. One export
per 24 h per account. Same RLS and `user_id` filters as every other read.

## 4. Questions for the owner / counsel before building

1. Which records must be retained after deletion, and for how long (billing
   ledgers, audit events, invoices)?
2. Grace period length (14 days proposed), and whether deletion is reversible
   during it.
3. Does a deletion request also trigger the privacy-policy "deletion request"
   purge for third-party lead data the user exported (we cannot recall files
   already downloaded; the policy wording should say so)?
4. Should the Stripe customer email be cleared, or kept for invoice delivery?

## 5. Size

Migration (deletion-request + tombstone columns, `account_deletions` outbox),
two routes, one beat task, one worker export job, Settings UI with the typed
confirmation. Roughly 3 to 4 PRs. Tests must cover: cross-tenant isolation of the
purge, idempotent resume, Stripe failure retry, restore during grace, and that
retained billing rows no longer carry PII.
