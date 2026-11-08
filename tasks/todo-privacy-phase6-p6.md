# Phase 6 P6: erasure procedure, artifact invalidation, second pass

Owner approval: "approved" 2026-11-08 (after P5 shipped). P7 (activation) is NOT approved.
Design: docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md section 7, D3
(delete = blank person data, keep a nameless property skeleton; opt-out = suppress only).
Precedent: the account-deletion purge (migration 113 + src/workers/account_deletion_beat.py).

Everything stays dormant: migration 125's guard keeps every rule 'pending', so no erasure
runs in production until P7. Tests insert active rules the way the P5 tests do.

## What erasure must reach (per active rule whose request kind = 'delete')

| Store | Action |
|---|---|
| results (any tenant) matched by the rule | Collect skip_trace_subject_hash first; then blank the purge's column list (party_name, heirs, legal_description, mailing_address, enrichment_data, phone, phone_type, phone_dnc_flag, email, phones, emails, owner_state, absentee_owner, out_of_state_owner, last_trace_outcome, skip_trace_subject_hash). Keep the property skeleton (parcel, address, dedup/property keys) for billing (D3). Stamp results.privacy_erased_at. |
| skip_trace_cache | Delete by the collected subject hashes |
| pending_skip_trace_rows | Delete unsent rows for the erased results; blank names + mail address on sent ones |
| skip_trace_queues | NULL download_url on queues that held the erased results' rows |
| king_parcel_owners | Rows naming the subject on the rule's parcel become tombstones (owner_name NULL, outcome privacy_withheld, rule id) |
| nts_notices | Notices whose grantor matches: grantor NULL, inactive, privacy_suppression_id (132's CHECK) |
| county_records | Delete matched rows (orphaned store) |
| R2 | See open question 1 |
| delivered_records, property_list_membership | Kept: property ledgers, no names (D3) |

Matching: a row counts as the subject's when authorize() against a snapshot of the active
DELETE rules says WITHHELD/REVIEW. Not by results.privacy_suppression_id alone: enforce_rows
never overwrites a flag, so a row first flagged by an opt-out rule for the same person
would carry the opt-out rule's id. Run enforce first so nothing unflagged is missed.

Co-owner rows (D2): the row is withheld whole; erasure blanks the whole row (the co-owner's
name too). Privacy-protective and consistent with D2; the row is never delivered anyway.

## Slices (each <= 5 files, Codex review each)

- [ ] P6a migration 135: results.privacy_erased_at; table privacy_erasures (rule_id PK,
      first_pass_at, r2_swept_at, second_pass_after, completed_at, lease token/expiry),
      RLS forced + system policy, no anon/authenticated grants (134 postcondition).
      If bridgeleads_system lacks a needed privilege (DELETE skip_trace_cache /
      county_records), add a SECURITY DEFINER function owned by bridgeleads_purge, as 113.
- [ ] P6b src/privacy/erase.py: idempotent batch procedure (above), one transaction per
      batch, lock order users -> rows (account-deletion fence compatible), skips accounts
      purging/deleted (their own purge covers them).
- [ ] P6c R2 invalidation (per open question 1).
- [ ] P6d beat task drive_privacy_erasures: advisory lock, lease, budget 240 s; first pass,
      R2 sweep, then a second pass >= 24 h later (after in-flight jobs drain); complete only
      if the second pass found nothing re-filled. Registered in scheduler + workers include.
- [ ] Tests on the test DB for each slice; ruff; full privacy suite.
- [ ] BUILD_JOURNAL, design doc section 7 status, handoff, memory.

## Codex consult 2026-11-08: reconciled

| Codex finding | Decision |
|---|---|
| Critical: discovery through enforce_rows misses rows already flagged by an opt-out rule | ADOPTED. Erasure discovers by itself: a keyset scan over ALL results (flagged or not), authorize() against a snapshot of the active DELETE rules only, fail closed if it cannot load. Requests are rare, so a full scan per rule is acceptable. |
| Critical: dialer_deliveries outbox omitted | ADOPTED. Pending/failed outbox rows for erased results are cancelled; delivered rows kept (ids only, no PII) and counted as "already delivered" in the erasure record. (P4 already re-authorizes before each send; this removes the queue too.) |
| High: R2 option (a) breaks live downloads | ADOPTED (c). Delete the object, keep the job downloadable: a marker `jobs.privacy_invalidated_at`; both download endpoints fall back to the DB render (which already excludes flagged rows) instead of 404. Same marker approach for batch_runs combined CSVs; account-export ZIPs are deleted (derived key, 7-day expiry, re-requestable). Never overwrite the same key (versions/caches). |
| High: lineage incomplete; contact_lookup_action_results | Cache hashes come from results.skip_trace_subject_hash AND pending_skip_trace_rows (pending_row_subject_key) for every erased result, collected before blanking. contact_lookup_action_results holds ids + disposition only (models.py:2159+): kept, no PII. Recorded, not changed. |
| High: run as a purge-owned definer function, not plain system SQL | ADOPTED. SECURITY DEFINER privacy_erase_batch(rule, result_ids) owned by bridgeleads_purge, fixed search_path, EXECUTE only to bridgeleads_system; Python keeps orchestration and leases. |
| High: users -> rows lock order does not serialize a cross-tenant erasure | ADOPTED. pg advisory xact lock per rule, then rows locked in id order. Results writers stay atomic via P5's `privacy_suppressed_at IS NULL` predicates (erased rows are always flagged first, in the same batch). |
| Medium: second-pass completion undefined | ADOPTED. Complete only when: >= 24 h after the first pass, no job created before the first pass is still non-terminal, the second sweep (results + shared stores) erased nothing, and no outbox/pending row for an erased result is non-terminal. Otherwise re-run later. |
| Medium Q3 | Same as the Critical above. |

## Open questions sent to the Codex consult (answered above)

1. R2. Job export download (jobs.py:2593) rebuilds the CSV from the DB (already filtered
   by the privacy flag) but refuses when jobs.export_key is NULL; the token download
   (jobs.py:2367) may stream the R2 object. Options: (a) delete the object and NULL
   export_key, as the purge does (customer loses the whole job's download over one
   person); (b) re-render the export without the subject and overwrite the same key;
   (c) delete the object but keep a marker so the download falls back to the DB render.
   Batch combined CSVs and account-export ZIPs (key derived, 7-day expiry) likewise.
2. Run as bridgeleads_system with plain SQL, or a definer function owned by
   bridgeleads_purge like 113?
3. Is matching via authorize() over flagged rows (any rule) right, or should erasure also
   sweep unflagged rows itself rather than rely on enforce_rows first?
4. Does the second pass need a write fence (like 113's zz_account_deletion_fence) to stop
   refills, or are P5's writer gates (privacy_suppressed_at IS NULL predicates) enough?
