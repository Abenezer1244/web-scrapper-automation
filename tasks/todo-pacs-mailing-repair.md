# Repair leads the PACS search-mode bug exhausted or wrongly settled (follow-up to #575)

Branch `fix/pacs-mailing-repair` (off main `e3c89860`, which has #575 and is deployed).
`scripts/repair_pacs_mailing_recovery.py`: a one-off operator script, dry-run by default.
`scripts/requeue_gis_mailing_recovery.py` cannot do this: it skips every row that carries a
`mailing_recovery_outcome`.

## Cohorts

A. RETRY COHORT. county IN (clallam, grant, jefferson, chelan, island, whatcom), WA, job done,
   mailing NULL, parcel >= 6 chars, `mailing_recovery_outcome = 'error'`, attempts >= 5, not
   deferred, no repair marker. `error` also covers genuine timeouts and ambiguous pages (the
   stored keys cannot tell them apart), so this is one more bounded recovery round, not a
   claim that each row was hit by the bug. Benton excluded (its lookups worked).
   ACTION: re-defer; remove `mailing_recovery_outcome` and `mailing_recovery_attempts`; keep
   `mailing_recovery_last_at` and every other key.
B. FALSE "NOT FOUND". island, WA, job done, mailing NULL, not deferred, no repair marker,
   outcome `parcel_not_found` from `pacs_island`, a parcel id with a letter (settled without a
   request). ACTION: remove the outcome, `mailing_source` and the attempt count; do NOT
   re-defer (#575 defers such ids without a request). The standard requeue then picks them
   up once Island's id format is supported.

## Safety
- Versioned marker `pacs_search_mode_repair_v1` (cohort, reason, time, previous outcome,
  attempts, last_at, source, deferral; built in SQL from the row at write time). Excluded in
  selection and in the UPDATE: a row is repaired at most once, its evidence never overwritten.
- Each UPDATE pins id, tenant, job, raw parcel id, mailing NULL, no marker, and the outcome /
  attempts / source / deferral values read for the report; any change makes it a no-op.
- Report (JSON lines: identifiers, county, cohort, previous outcome/attempts; no names or
  addresses) is complete on disk before any write.
- `--apply` needs `--deployed-commit <hex sha>` (the operator's attestation of what the worker
  runs, from `railway deployment list`), checked in this checkout to contain #575.
- `--apply` holds the sweeps' single-flight lock as an OWNED lease (token; Lua
  compare-and-expire / compare-and-delete), renewed on time (every 60 s, checked before every
  write), statements capped at 120 s, TTL 1200 s. Lost or unprovable ownership raises before
  the next write (exit 4); unreachable Redis or a held lock refuses to start (exit 3).
- No lookups; no mailing address, billing, quota, job or skip-trace writes.

## Run (owner)
1. `railway run --service worker python scripts/repair_pacs_mailing_recovery.py` (dry-run):
   read the counts and the evidence file.
2. If agreed: `... --apply --deployed-commit <worker sha>`; rerunnable after a stop.

## Steps
- [x] 1. Codex plan consult (NO-GO with 5 amendments, all built).
- [x] 2. Script + tests (26; mutation checks on the marker and the attempts guard).
- [ ] 3. Codex review to GO (round 1 NO-GO: lease ownership [High], '' key, stale runbook: fixed).
- [ ] 4. PR; owner runs the dry-run, reviews the report, decides on --apply.
