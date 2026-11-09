> **DRAFT, requires counsel review. Not legal advice. Not published.**

# BridgeLeads Data Retention Policy (draft, internal)

**Draft date:** 2026-11-09. **Facts checked against:** backend `main` at `7aff3c76`.
**Audience:** internal policy. The summary in section 7 of the Privacy Policy draft is derived from it.

## Drafting notes

- Built from `04-RETENTION-MATRIX.md` (2026-11-04), re-checked against current code:
  `src/config/settings.py` (retention settings), `src/workers/scheduler_helpers/retention.py` (the purge),
  `src/privacy/deadlines.py`, `src/workers/privacy_requests.py`, migrations 125, 135, 136, 137.
- **Code describes what happens; this policy decides what should.** Every period not set by the owner is
  `[OPEN]`. A period decided here is not real until the code enforces it. The "Code today" column must stay
  true; the "Policy" column is what the owner adopts.
- "Gated" means the job exists but runs only when `RETENTION_PURGE_ENABLED=true` and
  `RETENTION_PURGE_DRY_RUN=false`. The code defaults are `false` and `true`. `[OPEN: production values. An ops
  note dated 2026-10-30 says the purge was enabled on the worker; re-confirm.]`

## 1. Principles

1. Keep personal information only as long as there is a business or legal reason.
2. Each store has one owner, one period, one deletion mechanism, and a test.
3. A legal hold (section 4) overrides every period.
4. Deletion includes copies we control: database rows, stored files, caches. It cannot reach copies customers
   already downloaded or sent to their own tools, or data a vendor holds; those are handled by contract and
   notice.

## 2. Schedule

### 2.1 Customer data

| Data | Store | Code today | Policy |
|---|---|---|---|
| Account (email, password hash, MFA, API key hashes, sessions) | `users`, `user_sessions`, `password_history` | Life of account. Removed 30 days after a deletion request (gated by `ACCOUNT_DELETION_ENABLED`, default off) | As built `[OPEN: confirm the flag is on]` |
| Billing records | Stripe; `users.stripe_customer_id` | Kept. The deletion email says 7 years. The Stripe customer is deleted after the account purge. | `[OPEN: period, accountant to confirm]` |
| Security and audit events (IP, path, event) | `audit_events` (append-only) | Never deleted; only the `detail` field is scrubbed on account deletion | `[OPEN: proposed 1 year, then delete or truncate the IP]` |
| Application and worker logs | Railway; Loki | Vendor default; Loki retention not set | `[OPEN: match audit_events]` |
| Job logs | `job_logs` | Never deleted, except on account deletion | `[OPEN: proposed 90 days]` |
| Rate-limit keys (IP-derived) | Upstash Redis | Short TTL | As built |
| Terms and privacy acceptance | `legal_acceptances` (append-only) | `[OPEN: confirm behaviour on account deletion]` | `[COUNSEL: keep as evidence of acceptance; for how long]` |
| Promotional email opt-outs | address-keyed suppression | `[OPEN: confirm]` | Keep while we send promotional email |
| Trial-abuse marker (keyed hash of email) | `consumed_trial_emails` | 2 years | As built; disclose |
| Unverified signups | `pending_registrations` | Purged hourly after expiry | As built |
| Account data export ZIP | R2 | 7 days, then deleted | As built |
| Account deletion and export records | `account_deletions`, `account_exports` | "24 months" in the design, no job | `[OPEN: implement the 24-month expiry]` |
| Support email | Mailbox | `[OPEN: provider and retention]` | `[OPEN]` |
| Website analytics | Google Analytics; Vercel | GA property setting `[OPEN]`; Vercel 24-hour hash | `[OPEN: 2 or 14 months, GA setting]` |

### 2.2 Lead data (people named in public records)

| Data | Store | Code today | Policy |
|---|---|---|---|
| Lead rows (names, heirs, property and mailing address, record details, amounts) | `results` | **Never deleted.** The retention module records the owner decision to keep the public-record lead row and remove only purchased contact data. | `[OPEN: owner decision B1-4. The previously published policy said about 365 days, then deleted; that was not true. Choose a period, or keep indefinitely and say so.]` |
| Looked-up phones and emails on lead rows | `results` | Set to NULL 365 days after `skip_trace_attempted_at` (gated). Known defect: error paths re-stamp that column, which restarts the clock. | `[OPEN: 365 days from first successful lookup, after the clock fix; COUNSEL: which clock the law and the vendor contract require]` |
| Lookup cache, including the raw vendor response | `skip_trace_cache` | Deleted after 90 days (gated) | 90 days |
| Pending lookup rows (plain name and address) | `pending_skip_trace_rows` | Never purged (scrubbed on account deletion or privacy erasure only) | `[OPEN: proposed 30 days after the queue completes]` |
| Stored job and batch exports | R2 | Deleted after 365 days (gated) | `[OPEN: 90 to 365 days]` |
| Vendor result-file link | `skip_trace_queues.download_url` | Cleared 30 days after a completed queue (gated). The file itself stays at the vendor. | `[OPEN: shorten to about 1 day (audit P1-11); ask the vendor for deletion terms]` |
| Orphaned county records | `county_records` | Weekly purge after 365 days, always on | As built |
| Trustee-sale notices (grantor, amounts) | `nts_notices` | Never purged | `[OPEN: proposed 365 days after the auction date]` |
| King County owner lookups | `king_parcel_owners` | Never purged | `[OPEN: refresh-replace; drop rows not seen in N days]` |
| Public website sample | `public_sample_cache` | Overwritten hourly | As built, after it is made non-identifying (audit P1-6) |

### 2.3 Privacy request records

| Data | Store | Code today | Policy |
|---|---|---|---|
| Request envelope (kind, channel, dates, keyed hash of requester email) | `privacy_requests` | Kept; no deletion job | 24 months after close (owner decision D5) `[OPEN: no job enforces it]` |
| Requester contact and details (encrypted) | `privacy_request_payloads` | Kept; no deletion job. Stored in its own table so it can be deleted without touching the audit trail. | Delete 24 months after close (D5) `[OPEN: no job enforces it]` |
| Request audit trail | `privacy_request_events` (append-only) | Kept | 24 months after close (D5) `[OPEN: no job]` |
| Opt-out and deletion rules (keyed hashes, no names) | `privacy_suppressions` (append-only) | Kept. A request that has a rule cannot be deleted (database restriction). | Indefinitely (D5), so the person is not re-imported |
| Review releases | `privacy_review_releases` | Kept | Indefinitely, with the rule |
| Email confirmation tokens (hash only) | `privacy_request_tokens` | Deleted 30 days after expiry (links expire after 7 days) | As built |
| Outgoing privacy email queue | `privacy_email_outbox` | `[OPEN: no purge found]` | `[OPEN]` |
| Erasure progress | `privacy_erasures` | Kept | `[OPEN: with the request record]` |

`[COUNSEL: whether 24 months (taken from the CCPA regulations, D5) is the right record-keeping period, and
whether keeping a suppression rule indefinitely after a deletion request is permitted. The rule holds only
keyed hashes.]`

### 2.4 Backups

`[OPEN: Supabase backup and point-in-time recovery window. Policy should say deleted data ages out of backups
within N days, and that backups are not restored into production without re-applying deletions and privacy
rules.]`

## 3. How deletion is done

- Scheduled purges run as Celery beat tasks (`src/workers/scheduler.py`, `scheduler_helpers/retention.py`).
  Each purge runs in bounded batches with lock and statement timeouts. Dry-run mode logs what would be deleted
  and writes nothing.
- Privacy erasure for one person is described in the Privacy Request Procedure draft.
- Account deletion is a separate 30-day process (`src/workers/account_deletion_beat.py`).

## 4. Legal hold

`[OPEN: who can place a hold, how it is recorded, and how purges skip held data. No hold mechanism exists in
code. COUNSEL: triggers (litigation, regulator inquiry, security incident).]`

## 5. Review

`[OPEN: owner of this policy and review cadence.]`
