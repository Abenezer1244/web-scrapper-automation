# Runbook: the v2 lookup subject key cutover (migration 098)

Phase 1a of the contact-lookup work. Read `tasks/todo-lookup-contacts.md` first.

## What changes and why

Skip-trace answers were cached under an ADDRESS-ONLY key with no owner name. Inside the
90-day window that means one address has exactly one answer, so a lead can be served the
previous owner's phone. Probate makes that the common case: the deceased owner is traced,
an heir is scraped later, and the heir's lead inherits the dead owner's contacts.

After this cutover, reuse is keyed on the SUBJECT a lookup was bought for: the account, the
address, the trace type and the exact names sent to the provider.

**What it costs.** Legacy cache rows become inert. A repeat address may be paid for again,
once, inside the remaining 90 days of its entry. That self-heals, because the cache TTL is
90 days anyway. Leads settled before 098 also stop donating contacts to duplicate re-scrapes
(their `skip_trace_subject_hash` is NULL and fails closed); those fall through to the v2
cache read, which is free when the same subject really was traced before.

## Before you start

- [ ] Migration 098 is reviewed and ready but NOT yet applied.
- [ ] Confirm the current global kill switch value so you can put it back.
- [ ] Have the read-only prod checks below ready to paste.

## The cutover

The kill switch alone does NOT quiesce this. It gates the enqueue
(`tasks_helpers/enrich.py:2151`) and the dispatcher (`skip_trace_dispatcher.py:39`). It does
NOT gate ingest (`tracerfy_ingest.py:451`), the webhook that feeds it
(`api/routes/webhooks.py:132`), or `_reuse_enrichment_for_duplicates` (`enrich.py:768`).

**Ingest must keep running with the switch off.** A batch the provider already accepted is
paid work; blocking it would strand both the contacts and the billing state. The requirement
is not that ingest stops, it is that every ingest AFTER the cutover runs v2 code, including
for batches submitted before it.

1. [ ] Set `SKIP_TRACE_ENABLED=false` everywhere (API and worker).
2. [ ] Restart API, Celery workers and Beat, so no old process survives. A rolling deploy can
       otherwise leave an old worker submitting rows or writing legacy keys after the flag
       changed. This is the step most likely to be skipped and it is the one that matters.
3. [ ] Confirm no dispatcher is submitting and no old build remains: no rows in `submitting`,
       beat stopped.
4. [ ] Deploy the v2 build with the switch still false.
5. [ ] Let the NEW ingest drain every outstanding `submitted` / webhook batch.
6. [ ] Reconcile any `submitting` row whose outcome is unknown. Do NOT casually cancel one:
       a row that reached the provider may already have been charged.
7. [ ] Apply migration 098 (`scripts/migrate.py`, which takes the advisory lock; not bare
       alembic).
8. [ ] Verify by the OBJECTS, not by `alembic_version` (the app role reads that table as
       empty, so it proves nothing):

       SELECT column_name, data_type, character_maximum_length
         FROM information_schema.columns
        WHERE table_name='results' AND column_name='skip_trace_subject_hash';

       SELECT indexdef FROM pg_indexes
        WHERE indexname='ix_results_skip_trace_subject_hash';

9. [ ] Re-enable `SKIP_TRACE_ENABLED`.

Queued rows may simply stay queued through all of this; they are picked up afterwards. Paid
or possibly-paid rows are drained or reconciled, never merely flagged.

## Verifying the cutover from production

Do not assume the deploy took. Two signals were added for this:

- [ ] `v2_key_reads=N` on the enqueue log line (`Job <id> skip trace enqueue: ...`). It
      should track the eligible row count on every run.
- [ ] A WARNING from `scraper.enrichment.skip_trace` reading
      `LEGACY address_cache_key called`. After the cutover this should never appear. If it
      does, something still reads the old key: find it before re-enabling anything.

Then confirm reuse is still working rather than silently off:

- [ ] `SELECT count(*) FROM results WHERE skip_trace_subject_hash IS NOT NULL` climbs as
      lookups settle.
- [ ] `skip_trace_source='reused'` still appears; if it drops to zero for days, subjects are
      not matching and something is re-paying.

## Afterwards, as a SEPARATE step

Deleting the legacy cache rows is PII hygiene ONLY. It is never the correctness mechanism
(correctness comes from the new code not reading them), so it must not be bundled into the
cutover window.

- [ ] Count first, then delete, then record the deleted row count.
- [ ] Legacy rows are the ones no v2 read will ever match. Identify them by age and by the
      absence of any `results.skip_trace_subject_hash` equal to their `address_hash`.

## Ops scripts

Three scripts called the legacy key directly and were not in the original five-path list:

- `scripts/backfill_skip_trace_jobs.py` — switched to the v2 key; it also now records
  `skip_trace_subject_hash` on rows it settles from cache.
- `scripts/sprint4_enqueue_existing.py` — **DISABLED**. It is a one-off Sprint 4 migration
  that already ran, it reads the legacy key and writes the result onto the lead, and it
  substitutes the mailing address for the property address, so even re-keying it would key
  some rows to the wrong subject. Rewrite it against the current enqueue path before reviving.
- `scripts/verify_tracerfy_provenance.py` — prefers the row's recorded
  `skip_trace_subject_hash`, and falls back to the legacy key only for pre-098 rows. It is
  read-only forensics and never a reuse decision.

## Rollback

The migration is additive and nullable, so 098 itself does not need reverting to roll back
the code. Reverting the code restores legacy-key reads, and any answer bought while v2 was
live stays in the cache under its v2 key (it becomes unreadable to the old code, so those
addresses would be paid for again). Prefer rolling forward.
