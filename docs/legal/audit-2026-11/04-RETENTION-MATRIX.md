# Retention matrix: policy vs code vs proposal (2026-11-04)

"Gated" means the job exists but runs only with `RETENTION_PURGE_ENABLED=True` and `RETENTION_PURGE_DRY_RUN=False`.
The code defaults are False / True. An ops note from 2026-10-30 says the purge was enabled on the Railway worker;
the owner must re-confirm.

The **Proposed** column is an engineering suggestion for the owner to decide. It is not a legal requirement.

| Data | Policy says | Code does | Enforced? | Proposed (owner decides) | Notes / legal hold |
|---|---|---|---|---|---|
| Customer account (`users`) | Life of account, then "as required by law" | Tombstone 30 days after a deletion request (gated `ACCOUNT_DELETION_ENABLED`) | Gated | As built | Keep `stripe_customer_id` and billing linkage for tax and accounting |
| Billing records | (not stated) | Kept. The deletion email says 7 years. Stripe holds the invoices. | Partly | 7 years (accountant to confirm) | Tax law |
| Lead rows `results` (names, heirs, addresses, distress data) | "approximately 365 days, then deleted" | **Never deleted.** Only phone and email are NULLed after 365 days (gated). | **No: the policy is inaccurate** | Delete or anonymise lead rows N days after job completion. Option: keep parcel and record ref only. | The customer's exported copies are outside our control. Disclose that. |
| Skip-trace phones/emails on `results` | (implied 90 days via the cache line) | NULL after 365 days (gated). The clock is reset by error re-stamps. | Gated, with a clock defect | 365 days from first successful trace (fix the clock) | Tracerfy contract terms may set the limit |
| `skip_trace_cache` incl. `raw_response` | "cached for up to 90 days" | Delete after 90 days (gated). The read TTL is 90 days regardless. | Gated | 90 days; drop `raw_response` at ingest if it is not needed | |
| `pending_skip_trace_rows` (plaintext name and address) | (not stated) | **Never purged** (scrubbed only on account deletion) | No | 30 days after the queue completes | |
| R2 job and batch exports | (not stated) | Delete after 365 days (gated) | Gated | 90 to 365 days | |
| Tracerfy CDN result URL | (not stated) | Cleared at 30 days (gated). The file itself stays at Tracerfy. | Partly | Ask Tracerfy for deletion terms | Vendor |
| `county_records` | (covered by "lead records") | Weekly purge after 365 days, always on | **Yes** | As built (the table is orphaned; consider dropping it) | |
| `nts_notices` (grantor, amounts) | (not stated) | Never purged | No | 365 days after auction date | |
| `king_parcel_owners` | (not stated) | Never purged | No | Refresh-replace; drop rows not seen in N days | |
| `job_logs` | (not stated) | Never purged (deleted only on account deletion) | No | 90 days | May contain addresses in messages: check |
| `audit_events` (IP, path) | "Security logs are retained for [retention period]" | Never purged; survives account deletion | No | 1 year, then delete or truncate the IP | Hold for open incidents |
| App / worker logs (Railway, Loki) | (as above) | Vendor default; Loki retention unset | Unknown | Match `audit_events` | |
| `public_sample_cache` | (not stated) | Overwritten hourly | Yes | Make it non-identifying (P1-6) | |
| Account exports (ZIP) | (not stated) | 7 days, then deleted | Yes | As built | |
| `account_deletions` / `account_exports` rows | (not stated) | "24 months" in the design doc, no job | No | Implement the 24-month expiry | Evidence of rights fulfilment |
| `consumed_trial_emails` (HMAC) | (not stated) | 2 years | Yes | As built; disclose it | Fraud prevention |
| `pending_registrations` | (not stated) | Purged hourly after expiry | Yes | As built | |
| Backups / PITR | (not stated) | Supabase-managed, not handled in code | Unknown | Document the backup window and say deleted data ages out of backups within N days | |
| Analytics (GA4) | (not stated) | GA property setting | Unknown | 2 or 14 months (GA setting) | |
| Support messages | (not stated) | No support system in code; the mailbox does not exist yet | n/a | Decide when the mailbox exists | |
