# Phase 6 P5: write-path gates + enforcer sweep

Owner approved P5 on 2026-11-07 ("approved"). Design: docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md
section 6 ("Write paths"). Merge rule: CI green + Codex GO per slice. Branch feat/privacy-phase6-p5.
No rule can be active before P7 (migration 125 guard), so every gate below is dormant in prod
until P7; each must still be correct the moment a rule activates.

## Facts found before planning (write-path inventory, 2026-11-07)

- `authorize._subjects` reads only party_name (party) and heirs (heir). Exports also deliver
  `enrichment_data.assessor_current_owner` as "current_owner" (lead_export.py:690, written at
  enrich.py:2061). That is the owner-of-record subject the adapter must add. Trustee-sale
  party_name IS the NTS grantor (trustee_sale.py:170), so grantor is covered via party_name.
- `results` has NO updated_at: the sweep cannot be incremental by watermark.
- Insert: one site, tasks.py:1349 (`pg_insert(Result).on_conflict_do_nothing`, batches of 1000,
  rls_sync_session). `records` is list[ScrapedRecord] (no contacts); record_type/county/state
  from config. Living-TOD filter at tasks.py:1156 is the drop precedent.
- UPDATE writers with NO privacy predicate: owner_recovery `_CACHE_FILL_SQL`/`_write`,
  cv_owner_recovery (held branch), pierce_cv_owner_recovery, pierce_atip_owner.write_owner_decisions,
  mailing_recovery `_write_row`, cv_mailing_recovery.write_row, property_recovery `_write`,
  dedup `_apply_survivor_merge` (heirs!), enrich `_reuse_enrichment_for_duplicates` (2 stmts),
  enrich `_fill_king_owners_from_store`, inline enrichment owner/mailing fills.
- Flag-only (no authorize): copy_cached_answer, `_settle_queued_from_known_answers`,
  cv_late_delivery (`grant_records` bills before any authorize).
- Shared stores: king_parcel_owners (`_STORE_SQL`, keyed by PIN, fans out to every tenant);
  nts_notices (`_upsert_notice`, grantor COALESCE'd so an erasure is undone by the next crawl).
  county_records has no writer.
- Ingest (P4d) authorizes on the lead's stored contacts, not the incoming answer's.
- scripts/repair_orphaned_duplicate_flags.py ranks winners with no actionable predicate (gap 8c).
- Fence (migration 113): UPDATE takes no users lock and never refuses; privacy columns are not
  fenced, so the sweep can flag rows of a purging account. cv_late_delivery locks rows then users.

## Slices (each <= 5 files, own PR, CI green + Codex GO before merge)

- [ ] **P5a owner-of-record adapter.** `authorize._subjects` adds
  `enrichment_data.assessor_current_owner` (role owner_of_record, allowlisted keys in one tuple).
  Files: authorize.py, test. Activation blocker cleared.
- [ ] **P5b enforcer sweep (beat).** New `src/workers/privacy_enforcer.py`: load snapshot; if no
  active rules, return (one query). Else keyset-scan unflagged results (ORM, decrypted contacts)
  joined to job/config for record_type/county/state; authorize; flag non-ALLOWED with
  `UPDATE ... SET privacy_suppressed_at, privacy_suppression_id WHERE id=ANY AND
  privacy_suppressed_at IS NULL`. Redis single-flight lock; no users lock. Never clears a flag
  (lifting is P7). Files: privacy_enforcer.py, scheduler.py, workers/__init__.py, settings, test.
- [ ] **P5c insert gate.** run_scrape_job: load snapshot once before the insert loop
  (RuleSnapshotError fails the job, retryable), authorize each record, DROP non-ALLOWED before
  insert like the TOD filter, log counts only. Files: tasks.py, test.
- [ ] **P5d shared stores.** store_answers/_store: authorize {parcel_id, party_name=owner}; a
  withheld owner name is not stored. _upsert_notice: a withheld grantor is written as NULL and
  never restored. Files: owner_recovery.py, nts_crawler.py, tests.
- [ ] **P5e recovery-writer predicate** (two commits of <= 5 files): `AND privacy_suppressed_at
  IS NULL` on every UPDATE listed above. Static test that each writer's SQL carries it + one
  real-DB test.
- [ ] **P5f late delivery + repair script:** `_deliver_one` authorizes before `grant_records`
  (non-ALLOWED -> flag, never billed); repair script uses `actionable_sql`.
- [ ] **P5g cache copies + ingest incoming contacts:** copy_cached_answer,
  `_settle_queued_from_known_answers` and ingest authorize the row WITH the contacts about to be
  written.

## Codex consult (2026-11-07): APPROVE WITH CHANGES, all adopted (design silent -> Codex wins)

- Q1 insert gate drops every non-ALLOWED record (REVIEW too), counts only in logs.
- Q2 no sweep lag for writers that ADD a subject or contact: each authorizes the post-write row
  in the same transaction via one shared helper `src/privacy/enforce.py::enforce_rows(db,
  snapshot, result_ids)` (flags non-ALLOWED atomically, stamps the checked generation). The
  sweep is the backstop and reuses the helper.
- Q3 no indefinite full scan: migration 130 adds `results.privacy_checked_gen` (bigint) +
  partial index WHERE privacy_suppressed_at IS NULL. Generation = number of rule rows (the
  table is append-only, every state change is a row, so it only grows). Sweep scans rows whose
  gen is stale; writers that change subjects reset or stamp it.
- Q4 king_parcel_owners: outcome 'privacy_withheld' (no owner name) widened in 130's CHECK.
  Clearing it when a rule is lifted is P7 (lifting).
- Fence (Codex High): the helper and the sweep skip rows of accounts that are purging/deleted
  (app-level refusal), lock order users -> rows preserved (no users lock taken).
- Critical x2 (late delivery bills first; repair script promotes suppressed rows) -> P5g.
- High: an explicit write-path MANIFEST (below) and a test per writer, not a static SQL grep.

## Revised slices (supersede the list above)

- [x] P5a owner-of-record adapter + projection guard (PR #602, Codex GO).
- [x] P5b migration 130 (PR #602, Codex GO round 3).
- [x] P5c enforce_rows + generation + enforcer beat (Codex GO round 2: unscoped rows removed,
  every row decided with its job's config; RLS checked in prod pg_policies: *_system
  USING (true) for bridgeleads_system).
- [x] P5d insert gate (Codex GO).
- P5e consult (Codex, APPROVE WITH CHANGES, adopted): a nameless King tax lead waiting for
  its owner carries the owner's property + mailing, and authorize() cannot see a withheld
  owner it has no name for. So the tombstone carries its rule: migration 131
  `king_parcel_owners.privacy_suppression_id` (FK RESTRICT, CHECK outcome
  'privacy_withheld' <=> id set). Readers treat a tombstone as TERMINAL at any age (no
  county request, no name copied) and, in the same transaction, FLAG every unflagged result
  of that parcel still WAITING for its owner (blank/placeholder name: exactly the rows the
  fill would have named) with the tombstone's id + current generation. A row naming someone
  else on that parcel stays allowed (design: a shared parcel never suppresses others).
  Writers: one snapshot per operation, fail closed; WITHHELD and REVIEW both tombstone.
  NTS: suppressed grantor written NULL on insert AND on conflict (explicit, not COALESCE);
  tests: insert, conflict, repeat crawl, later crawl with parser NULL. Lifting (restoring a
  tombstoned parcel or a grantor) is P7. 131 + readers = ONE PR, deployed before writers.
- [ ] P5e1 King tombstone READERS first (Codex round 2): `_CANDIDATE_PARCELS_SQL` excludes
  fresh tombstones, `_fill_from_store` / `_fill_king_owners_from_store` / `fresh_answers`
  treat one as terminal (no name, no county request). Merge, deploy, verify every
  api/worker/beat runs it.
- [ ] P5e2 only then the WRITERS (store_answers/_store tombstone; nts_crawler upsert: blank +
  refuse grantor). Also dormant by construction: a tombstone is written only for a rule
  that is active, which migration 125's guard forbids until P7.
- [ ] P5f writers, inline enforce (2-3 PRs by manifest).
- [ ] P5g late delivery authorize-before-grant + repair script actionable_sql + authorize.
- [ ] P5h cache copies + ingest authorizes with the incoming contacts.

## Review log

- P5a Codex GO. Then found myself: two P4 call sites pass authorize() a column PROJECTION
  (skip_trace_dispatcher person_cols, tracerfy_ingest) without enrichment_data, so the new
  owner subject read as absent = allow. Fixed both; authorize() now refuses (RuleSnapshotError)
  a Mapping row missing any of ROW_COLUMNS while a rule is active, so a future projection
  bug fails closed.
- 130 Codex NO-GO round 1, adopted: idempotent pre-index steps (verified by simulating a
  partial apply then `upgrade head`), bigint, downgrade refuses while tombstones exist.
  Codex High "old readers mishandle privacy_withheld": no writer exists until P5e, so the
  reader fixes (exclude fresh tombstones from `_CANDIDATE_PARCELS_SQL`, terminal in
  `_fill_from_store` / `_fill_king_owners_from_store` / `fresh_answers`) ship IN P5e with
  the writer, with tests proving no county request and no name write.
  Generation source: kept "count of active rule rows" read in the SAME statement as the
  snapshot (a separate sequence could disagree with the snapshot it labels); history tests
  ship with enforce_rows (P5c).
- Local: tests/test_cv_late_dialer.py 3 tests need Redis (Celery .delay); none local. They
  fail identically without these changes; CI has Redis.

## Status 2026-11-07 (evening)
- #602 (P5a+P5b, migration 130) MERGED + deployed (129->130, all SUCCESS, /health 200).
- #603 (P5c enforcer + P5d insert gate) MERGED + deployed; the enforcer's first prod tick
  19:19:00 returned generation 0 in 0.13 s.
- #604 (P5e1 readers, migrations 131+132) open; P5e2 writers (Codex GO) on branch
  feat/privacy-phase6-p5e2, ships only after #604 is deployed.

- P5f-P5h built on feat/privacy-phase6-p5f (stacked on p5e2): job gate, owner sweeps,
  cached/Tracerfy answers with incoming contacts, late delivery, repair script (gap 8c
  closed), mailing/property predicates. All Codex GO.
- Merge order: #604 (readers) -> P5e2 writers PR -> P5f-h PR, each CI green + deploy verified.

## P5f-P5h consult (Codex, APPROVE WITH CHANGES, adopted)
- Job gate: ONE enforce_rows over the job's results AFTER every in-job writer (inline
  enrichment, NTS match, survivor reconciliation/merges, claim transfer), immediately
  before the post-enrichment refetch. RuleSnapshotError: retry the task; after retries the
  job fails; never cap/enqueue/bill/notify/deliver. Failed jobs' rows are not customer
  visible (results need done). The first R2 export is provisional and must be overwritten
  by the gated re-export before done (add a test).
- trustee_sale_finalize._apply_survivor_merge is a job writer: covered by the job gate,
  tested.
- Incoming contacts: tracerfy_ingest, _settle_queued_from_known_answers and the API
  contact_lookup_action copy authorize WITH the incoming phones/emails before writing.
- Beat writers: predicate + enforce_rows on RETURNING ids in the same transaction
  (owner_recovery _write and cache fill, cv/pierce owner writers, Pierce ATIP); mailing /
  property recovery predicate only.
- Operator scripts: out of P5, held by P7's activation gate test (all 33 must be filtered
  or deleted before any rule can activate).

## Write-path manifest (each row ends with its gate + test)
| Writer | Store | Gate |
|---|---|---|
| tasks.run_scrape_job insert | results | P5d drop |
| owner_recovery._CACHE_FILL_SQL, _write | results.party_name | P5f enforce_rows |
| owner_recovery.store_answers/_store | king_parcel_owners | P5e tombstone |
| cv_owner_recovery._write | results.party_name | P5f |
| pierce_cv_owner_recovery._write | results.party_name | P5f |
| pierce_atip_owner.write_owner_decisions | results.party_name | P5f |
| mailing_recovery._write_row, cv_mailing_recovery.write_row | results.mailing_address | P5f predicate |
| property_recovery._write | results.property_address | P5f predicate |
| dedup._apply_survivor_merge | results.heirs/enrichment_data | P5f |
| enrich._reuse_enrichment_for_duplicates (2) | results contacts | P5f |
| enrich._fill_king_owners_from_store, inline owner fills, king_parcel_locate.apply_owner_names | results.party_name, enrichment_data owner | P5f |
| nts_crawler._upsert_notice | nts_notices.grantor | P5e |
| cv_late_delivery._deliver_one | billing + unhold | P5g |
| scripts/repair_orphaned_duplicate_flags.py | delivered_records, is_duplicate | P5g |
| enrich.copy_cached_answer, skip_trace_dispatcher._settle_queued_from_known_answers | results contacts | P5h |
| tracerfy_ingest (incoming contacts) | results contacts, cache | P5h |

## Open questions for Codex (answered above)
1. Insert gate: drop non-ALLOWED (TOD precedent, PII never stored again) or insert flagged?
   REVIEW too?
2. Window: a writer that ADDS a subject (owner fill, heirs merge) can make an unflagged row
   suppressible; reads see it until the next sweep. Accept sweep lag, or authorize inline in
   those writers too?
3. Sweep cost: full scan of unflagged rows per tick while any rule is active (no updated_at).
   Acceptable, or add a column/trigger?
4. king_parcel_owners outcome for a withheld owner (avoid re-querying the county forever).
5. Anything missed.
