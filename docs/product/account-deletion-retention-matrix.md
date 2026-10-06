# Account deletion: retention matrix (P3a)

**Status:** proposal for owner sign-off, 2026-10-06 (Codex matrix review round 1 folded in). Built from the live schema
(alembic 112, `pg_constraint` / `information_schema` of the test database at the same
revision), not from memory. It is the acceptance criterion for the P3b purge: every
table and store below must end in exactly the state its row says.

Policy it implements: `account-deletion-and-export.md` §4.1. Plan + review log:
`tasks/todo-account-deletion.md`.

## 1. The constraint that shapes everything

The billing ledgers we must keep for 7 years hang off rows we would otherwise delete,
through `ON DELETE CASCADE` foreign keys:

| Deleting... | ...would CASCADE-delete (billing evidence) |
|---|---|
| `jobs` | `contact_lookup_actions` -> `contact_lookup_action_results`, `contact_lookup_action_events`; `skip_trace_queues`; `pending_skip_trace_rows`; `results` |
| `results` | `contact_lookup_action_results`, `pending_skip_trace_rows`, `dialer_deliveries` |
| `scraper_configs` | `jobs` (and everything above), `dialer_deliveries`, `user_record_views` |
| `scraper_batches` | `scraper_configs` (and everything above), `batch_runs` |

So the purge **cannot delete jobs, results, configs or batches** without destroying the
evidence §4.1 keeps. Instead it **keeps those rows as skeletons and blanks every
personal-data column in them** (third-party PII and the customer's own settings). The
billing linkage (ids, counts, amounts, timestamps, statuses) survives intact.

The alternative, changing those foreign keys so the ledgers survive a parent delete, is
a schema change on billing-critical tables; not recommended for this feature.

Other catalog facts: **no foreign key is DEFERRABLE**. One table is owned only
indirectly: `job_logs` (job_id, no user_id); its fence trigger resolves the owner
through `jobs`.

## 2. Per table

Legend: **DELETE** rows; **SCRUB** keep row, NULL/blank the listed columns; **KEEP**
unchanged (until its retention expiry, a later phase); **TOMBSTONE** see the users row.

| Table | Action | Detail |
|---|---|---|
| `users` | TOMBSTONE | Capture the original email HMAC first (for `pending_registrations` and the trial table). email -> `deleted+<id>@invalid` (recomputes email_hmac, frees the address); name, first_name, last_name, timezone, notification_prefs -> NULL/default; password_hash -> unusable random; mfa_* cleared; api_key_hash NULL; referral_code NULL; is_admin false; is_active false; deletion_state `deleted`. Kept: id, plan, stripe_customer_id, stripe_subscription_id, subscription_status, billing/quota/entitlement columns, created_at, referred_by_user_id (referral credit evidence, with referral_events). |
| `pending_registrations` | DELETE matching | No FK to users: raw email + names of a sign-up in progress. Delete rows whose email_hmac equals the captured original. |
| `user_avatars`, `user_sessions`, `pending_email_changes`, `password_history`, `mfa_backup_codes`, `mfa_break_glass_codes` | DELETE | Own credentials/profile. |
| `notifications`, `user_record_views`, `property_list_membership`, `job_logs`, `batch_runs`, `dialer_deliveries` | DELETE | No dependents need them; job_logs messages can carry addresses; batch_runs only after its R2 export is swept. |
| `scraper_batches` | SCRUB | name -> 'deleted', fields/enrichment/schedule/deliver -> empty JSON, delivery_mode NULL, status 'archived'. (deliver can hold emails and webhook targets.) Kept: state (billing context). |
| `scraper_configs` | SCRUB | name -> 'deleted', fields/enrichment/schedule/deliver/doc_types -> empty, include_living_owner_tod default, active false (already paused at request). Kept: county, state, record_type, skip_trace_enabled (what the jobs were billed for; not personal data). |
| `jobs` | SCRUB | export_key NULL (after the R2 delete), error_message NULL. Kept: status, counts, billed_count, billing_applied_at, dates, breakdown counts. |
| `results` | SCRUB | NULL: party_name, heirs, legal_description, mailing_address, enrichment_data, phone, phone_type, phone_dnc_flag, email, phones, emails, owner_state, absentee_owner, out_of_state_owner, last_trace_outcome, skip_trace_subject_hash (after the cache delete). Kept: ids, job_id, parcel_id, property_address/city/zip/state, date_recorded(+parsed), doc_type, dedup/duplicate fields, skip_trace_status, public-record amounts/dates. **Owner sign-off item: property address + parcel are kept** (county public record; needed to show what was billed and to match a homeowner's request). |
| `pending_skip_trace_rows` | SCRUB | NULL: first_name, last_name, mail_address, mail_city, mail_state, mail_zip. Kept: address, status, trace_type, queue/action ids (billing). Cache keys computed BEFORE this (see skip_trace_cache). |
| `skip_trace_queues` | SCRUB | download_url NULL (vendor link to a CSV of PII), error_message NULL (vendor text may echo input). Kept: counts, credits, statuses. |
| `contact_lookup_actions`, `contact_lookup_action_results`, `contact_lookup_action_events`, `skip_trace_meter_events`, `referral_events` | KEEP 7 y | Billing ledgers. No third-party PII columns; their text fields (status_reason, reason, disposition_reason/reference, quote_snapshot) are system-generated, never user or vendor input (P3b test asserts this on real rows). |
| `delivered_records` | KEEP 24 mo | Who received which parcel: forwards a homeowner's later deletion request. Holds parcel + address only. |
| `audit_events` | SCRUB, then KEEP 12/24 mo | detail NULL for the user's rows (free text). event, ip, path, created_at kept; expiry by event class is a later phase. |
| `account_deletions` | KEEP 24 mo | The record that the deletion happened (CCPA request log). |
| `consumed_trial_emails` | KEEP 2 y | Email HMAC only, only if the account used its trial. **Owner sign-off item: trial-fraud exception** (otherwise delete + re-register earns a second trial). |
| `skip_trace_cache` | DELETE matching | Keyed per user (`lookup_subject_key(user_id, address, names...)`), no user_id column. The cache is written ONLY at ingest, from a pending row (`tracerfy_ingest.py`, `pending_row_subject_key`), and pending rows are kept for billing, so they are the complete manifest of this user's cache keys. Delete rows whose key is any `pending_row_subject_key(row)` of the user's pending rows (computed BEFORE they are scrubbed) or any `results.skip_trace_subject_hash`. P3b test pins the only-written-from-a-pending-row invariant. |

**Skeleton lifetime:** the SCRUB skeletons (`jobs`, `results`, `scraper_configs`, `scraper_batches`, `pending_skip_trace_rows`, `skip_trace_queues`) and the tombstoned `users` row are deleted when the last KEEP row that depends on them expires (7 years after the last billing event). Until then nothing may physically delete the users row (no role holds DELETE on it; asserted since migration 112).

**Shared tables are out of scope:** `county_records`, `nts_notices`, `public_sample_cache` hold public-record data with no owner and are never touched by one account's purge. Homeowner requests against them are the separate suppression project.

## 3. Outside Postgres

| Store | Action |
|---|---|
| R2 | Delete every object under `exports/{user_id}/` (paginated list) AND every key referenced by the user's `jobs.export_key` / `batch_runs.combined_export_key`, twice: right after the claim and >= 24 h later; NULL the DB keys only after the delete succeeds; complete only when the prefix lists empty. |
| Vendor copies (Tracerfy uploads, a customer's own dialer) | Not deletable by us. Tracerfy: **owner/counsel item**, check the vendor contract for a deletion request path. Dialer pushes went into the customer's own system, like a downloaded file. |
| Stripe | Subscription set to cancel at period end at request time (beat); Customer deleted only after the subscription has ended and nothing is open; never redacted (invoices keep their own frozen copy of the customer details). |
| Redis | Revocation cutoffs, rate-limit and MFA-guard keys expire by TTL; nothing holds PII. No action. |
| Celery results / queues | Payloads carry ids only; results expire by TTL. No action. |
| Railway logs | Provider retention (days); logs carry ids, not lead PII. No action, documented. |
| Supabase backups / PITR | Age out on the provider's window (to confirm on the current plan); a restore must re-run tombstoned purges before serving. Documented, not automated. |
| Emails already sent, files already downloaded | Cannot be recalled; policy wording (design §4.3). |

## 4. Acceptance tests the purge must pass (P3b)

0. No `pending_registrations` row with the original email HMAC remains.
1. Another tenant's rows in every table above are byte-identical before and after.
2. Every SCRUB column is NULL/blank; every KEEP ledger row count is unchanged.
3. No row in any DELETE table remains for the user.
4. No skip_trace_cache row matching the user's keys remains; other users' rows remain.
5. R2 prefix lists empty after the final sweep, and every key the user's `jobs.export_key` / `batch_runs.combined_export_key` referenced (captured before any delete) returns 404.
6. A crash after any phase resumes to the same end state (idempotent re-run).
7. The users row matches the TOMBSTONE spec and the original address can register again.

## 5. Owner decisions (2026-10-06)

1. Lead shells keep property address + parcel (public record); everything else personal is blanked.
2. `consumed_trial_emails` 2-year HMAC exception approved.
3. Tracerfy's copies: no vendor call in the purge; added to the counsel list (contract deletion path).
4. Rollout: the owner switches `ACCOUNT_DELETION_ENABLED` on only after P3 (purge), P4 (export) and
   P5 (Settings UI) are live and verified.
