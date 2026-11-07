# Phase 6 P4: durable send authorization + PII-emission inventory

Owner approved P4 on 2026-11-06 ("go"). Design: docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md
section 6 ("Queued sends"). Merge rule: CI green + Codex GO per slice.

## Facts found before planning

- Prod workers' sync engine connects as `bridgeleads_system` (NOSUPERUSER, NOBYPASSRLS). Migration 125
  revoked every privilege on `privacy_suppressions` from it and enabled RLS with no policy. So
  `authorize.load_rule_snapshot` would raise (fail closed: ALL sends stop) and a bare GRANT SELECT
  would return ZERO rows under RLS (fail OPEN: every row allowed). Neither is acceptable.
- Dialer, generic webhook: the sweep selects rows (already filtered by the P3 flag via
  `actionable_condition()`), builds the full lead payload and seals it into a Celery message
  (`scheduler_helpers/dialer.py:336`, `cv_late_delivery.py:491`). Retries resend the frozen payload
  for up to `_MAX_RETRIES` with backoff, so a rule that activates meanwhile is never honoured.
- Dialer, outbox vendor (PhoneBurner): `dialer_outbox.process_dialer_outbox` already re-reads each
  Result by id at send time with `actionable_condition()`; a flagged row comes back None and is
  mislabelled "tax lead past 18-month cap".
- Tracerfy dispatcher: candidates join Result and filter `not_(withheld_condition())`;
  `_partition_still_deliverable` (skip_trace_dispatcher.py:1346) re-reads Result+Job FOR SHARE right
  before the claim commit; the POST runs after the commit. `_build_payload_rows` reads only the
  pending row. Withdraw path: `_cancel_undeliverable` (pending -> cancelled, Result queued ->
  not_attempted, never billed).
- Tracerfy ingest (tracerfy_ingest.py:809-923): per matched CSV row writes SkipTraceCache (subject
  hash key) and Result contact columns, pending -> completed; billing counts 'completed' (and
  'unmatched' when the queue accepted all) in the same transaction.
- Cache copies without a Tracerfy call (`_settle_queued_from_known_answers`, `enrich.copy_cached_answer`,
  duplicate reuse) are WRITE paths: P5 (write gates), not P4.

## Slices (each <= 5 files, own PR, CI green + Codex GO before merge)

- [x] **P4a: rule read path (expand-only, ships alone first).** Migration 127: SECURITY DEFINER
  `public.privacy_rule_rows()` returning exactly the loader's columns (no request_id, no payloads),
  `SET search_path = public, pg_temp`, REVOKE from PUBLIC, GRANT EXECUTE to `bridgeleads_system`
  (guarded IF EXISTS, as 125 does). `load_rule_snapshot` reads through the function. Tests: function
  returns rows despite RLS for a non-owner NOBYPASSRLS role; no table privilege granted.
- [x] **P4b: dialer.** Generic webhook carries result ids + total, not people: a new task rebuilds
  the payload at send (and on every retry) from Result by id with the P3 predicates, loads one
  snapshot and authorizes each row; URL/secret re-read from the config (owner-matched, plan
  re-checked). Outbox: authorize before build; flagged/withheld rows -> 'skipped' with an honest
  reason. Sweep + late delivery: authorize at selection too (one snapshot per sweep).
- [x] **P4c: Tracerfy dispatcher.** `_partition_still_deliverable` authorizes each row (it already
  holds Result FOR SHARE, so Result columns + config come from the same read); non-ALLOWED ->
  `_cancel_undeliverable`. Snapshot loaded once per tick; RuleSnapshotError aborts the tick
  (nothing claimed, nothing posted).
- [x] **P4d: ingest quarantine.** Before the cache/Result writes, authorize the matched Result;
  non-ALLOWED -> write no cache entry and no contact columns, pending -> 'quarantined' (not billed),
  Result -> 'errored'? (Codex question). RuleSnapshotError -> raise, transaction rolls back, ingest
  retries.
- [x] **P4e: PII-emission inventory** (scripts/, admin routes, ops alerts, logs): doc
  `docs/legal/audit-2026-11/08-PII-EMISSION-INVENTORY.md`, fixes only where trivial.

## Open questions for Codex
1. SECURITY DEFINER function vs table GRANT + RLS policy for `bridgeleads_system`.
2. Generic dialer: one new task vs a new kwarg on deliver_job_webhook; retry semantics.
3. Ingest quarantine: pending status + Result status + billing (Tracerfy charged us; customer must
   not be charged for data we will not deliver?).
4. Anything missed: other send sites, lock order with the account-deletion fence.

## Codex consult (2026-11-06) and reconciliation

Verdict: approve direction. Adopted:
- Q1 SECURITY DEFINER `privacy_rule_rows()`, schema-qualified, `search_path = public, pg_temp`,
  REVOKE PUBLIC, EXECUTE to bridgeleads_system only (029 pattern).
- Q2 a NEW task for the generic dialer (deliver_job_webhook also serves completion webhooks). It
  carries ids only, re-reads config + rows and authorizes on every attempt, keeps the SSRF check.
  Delivery stays AT-LEAST-ONCE, as today: the existing task already retries the same payload and the
  payload's documented contract is dedup by `batch.id` / `external_id`. P4 does not widen that window;
  documented in the task docstring rather than adding a new delivery-state table (Codex offered both).
- Q3 ingest: pending -> 'quarantined', Result -> 'errored', no cache, no contact columns; billing
  excludes 'quarantined' (provider-paid, customer-not-charged; counted in the job log for audit).
- Critical: `RuleSnapshotError` aborts the WHOLE operation and leaves rows retryable. Never a per-row
  failed/skipped/errored. Outbox: load the snapshot BEFORE the per-row loop (rows untouched on error).
  Generic task: snapshot failure retries. Dispatcher: load before the claim; error leaves rows queued.
  Ingest: raise, roll back, ingest retries.
- Dispatcher authorization happens inside `_partition_still_deliverable`, i.e. before the
  'submitting' claim commit.
- Honest reason codes ("privacy withheld"/"privacy review"), ids and counts only in logs.
- Deploy order: 127 alone first; send paths after. IMPLEMENTED_ENFORCEMENT_VERSION untouched (P7).

Answered, not changed:
- Lock order: authorize() is pure in-memory over rows the caller already holds; the snapshot is one
  SELECT through the function (AccessShare only). P4 adds no row lock, so no new lock cycle. The
  canonical order for activation cancellation is P7's design item.
- Activation-time cancellation of queued outbox/pending rows is P7 (design section 7). P4's send-time
  check already blocks those sends once a rule is active.
- Late-answer race (rule activates after ingest loaded its snapshot): the P5 enforcer flags the row,
  hiding it on every read path. Rules cannot activate before P7 anyway.
- Frozen export artifacts are P6 (erasure), per the design's order of work.

## Status (2026-11-06)

- P4e MERGED (#600): inventory doc + named ignore rules + activation gate test. Codex GO (5 rounds).
- P4a Codex GO (2 rounds). Migration 129 revises 128, so it merges after the NTS stack #598/#599.
- P4b/P4c Codex GO (2 rounds): outbox, id-only generic push (legacy sealed dialer payloads
  re-routed by id), sweep and late delivery load rules before any claim, dispatcher authorizes
  before the claim. authorize._get reads any Mapping.
- P4d: ingest quarantine ('quarantined' pending status, never billed, Result 'errored', the
  subject's cache entry dropped); the contact-lookup reconciler settles it as 'released'.

## Carried to P5 (write paths, design section 6)
- Late delivery (cv_late_delivery.grant_records) bills a held row before any authorize(): the P5
  write gate must authorize before the grant (Codex P4b review, deferred per the design).
- Cache copies that write contacts without Tracerfy: _settle_queued_from_known_answers,
  enrich.copy_cached_answer, duplicate reuse.
