# Live King jobs settle PINs King does not carry (follow-up A to #565 / #571)

Branch `fix/king-live-job-not-carried` (off main `c5b80a51`, which has #571's
`confirm_not_carried` and `resolve_pins(refresh=...)`). Owner chose to build it after Codex's
NO-GO on the first sketch; this plan is built around Codex's minimum requirements:
conservative identity, crash-safe ordering, bounded GIS, guarded writes, owner-pass suppression.

## Today

A live job's no_record parcel is marked `mailing_lookup_deferred` (and later
`property_lookup_deferred` if it has no property address); the mailing sweep then settles it
on its next tick (one more page request) and the property sweep would too. A tax_delinquent
job's owner-only pass also asks the page again for it.

## Design

1. `_fill_king_mailing_from_extract(pid_map, job_id, answers_out=None)` also hands back the
   answers it already read (`{"answers": ..., "snapshot": ...}`). No usable extract: nothing.
2. `_apply_king` records the pids whose phase-1 data is `parcel_lookup == "no_record"`.
3. CRASH-SAFE ORDER: the existing deferred markers are written and committed FIRST, exactly as
   today (no_record pids included). Only then a new helper swaps proven rows from deferred to
   settled with one guarded UPDATE per pid. A crash or failure before or during the swap leaves
   the rows deferred, i.e. today's behaviour; nothing is ever neither deferred nor settled.
4. ELIGIBILITY (conservative): the pid is exactly 10 digits; phase 1 labelled THAT pid
   no_record; EVERY row on it has no mailing address and NO resolved identity at all
   (`resolved_by`, `resolved_parcel_id`, `source_parcel_id` absent); the extract (loaded) says
   `absent` for that exact pid.
5. BOUNDED GIS: candidates in batches of 50 (one county_gis chunk = one request), each batch
   only while `_king_left() >= 45` (the existing King budget; no new allowance). Skip entirely
   when <= 0. Unproven candidates stay deferred.
6. GUARDED WRITE (`_SQL_SETTLE_NOT_CARRIED`): WHERE id IN rows AND user_id AND job_id AND
   btrim(parcel_id) = pid AND mailing_address IS NULL AND mergeable AND still
   `mailing_lookup_deferred = true` AND no resolved identity in the DB copy. SET merges
   `mailing_lookup_deferred=false, mailing_recovery_outcome=parcel_not_found,
   mailing_source=NOT_CARRIED_SOURCE, mailing_rpacct_snapshot=<snapshot>`, plus, only while the
   property address is empty, `property_lookup_outcome=parcel_not_found,
   property_lookup_deferred=false`. No `mailing_recovery_attempts`: no recovery attempt was
   made (decided, not implied). One commit; then `db.expire(res, ["enrichment_data"])` on every
   row of the job's pid so no later ORM flush (owner pass, property marker) writes a stale copy
   back. A pid counts as settled only if every one of its rows was updated.
7. After the swap: settled pids leave the in-memory `deferred` / `mailing_pending` counts
   (user-facing "still being looked up" line), and one factual user line says how many leads
   name a parcel King County has no record of.
8. OWNER PASS: rows actually settled (by id) are left out of `owner_needs`. No owner outcome
   is written (a parcel that does not exist is not "verified page, no owner").
9. Later `_mark_king_property_deferred` already skips them (#571 SQL guard + ORM skip on
   `PROPERTY_SETTLED_OUTCOMES`, and the ORM copy is now fresh after expire).

## Tests (real DB; HTTP boundaries only where possible)

- Helper level (`_settle_king_not_carried`): settles an eligible pid; leaves deferred when any
  of: resolved identity on any row, a row with mailing, not 10 digits, extract None or not
  absent, GIS present/unreached, no time left, row changed in the DB meanwhile (guard); the
  property keys only when the property address is empty; batches of 50 with a time check
  between; ORM copies are fresh afterwards.
- Job level: a King job whose page is "No data found." ends with the row settled, not
  deferred, and the owner-only pass does not ask about it (real `batch_enrich_king_county`
  with HTTP faked, if the existing job-level King tests allow it; otherwise the established
  substitution).

## Codex plan consult 2 (NO-GO as written; "would support implementation" with these made explicit)

Folded in, and these override the design above where they differ:
- WHOLE PARCEL: eligibility is judged over ALL of the job's rows on the pid (`all_results`,
  including hidden already-delivered rows), not `pid_map`. Any hidden row, any row with a
  mailing address, any resolved identity, or any POSITIVE evidence the parcel exists
  (a property address, `property_source`, `vacant_no_situs = true` from a GIS match, a
  `condo_unit_status` other than absent) makes the pid ineligible. The SQL repeats it with a
  `NOT EXISTS` over the job's same-pid siblings. "Absent" identity = missing, JSON null or "".
- ALL OR NOTHING per pid: a savepoint, `UPDATE ... RETURNING id`, and roll the pid back unless
  the returned ids are exactly its rows. Only committed row ids feed the owner exclusion,
  counts and the log line.
- PIN shape: ASCII `[0-9]{10}` (not `\d`) in Python and `'^[0-9]{10}$'` in SQL.
- BUDGET wording: one GIS request per 50-pid batch with a 30 s SOCKET timeout (connect/read),
  not a hard wall clock; a slow body can overrun. Batches only while `_king_left() >= 45`.
- Settlement runs only if the deferred-marker commit succeeded. `db.flush()` before the raw
  SQL, `db.rollback()` on failure (rows stay deferred), expire every loaded row on the pid
  after commit.
- OWNER RECOVERY: its `_ELIGIBLE_ROW` also excludes rows settled with
  `mailing_recovery_outcome = parcel_not_found` AND `mailing_source = NOT_CARRIED_SOURCE`
  (this covers rows the mailing sweep settled under #565 too). No owner outcome is invented.
- User line after commit, from committed counts: "King County sources did not find the parcel
  IDs listed on N leads." Pending counts drop only for fully settled pids.

## Steps
- [x] 1. Codex plan consult (NO-GO with fixes, folded in above).
- [x] 2. Helper + SQL + `_fill_king_mailing_from_extract` out-param, helper tests.
- [x] 3. Wired into the King block (label collection, swap after markers, counts, log, owner pass), job test.
- [x] 4. ruff, OpenAPI, import, related suites (626 passed). Codex diff review GO.

## Review (2026-10-31)

- enrich.py: `_settle_king_not_carried` + `_SQL_SETTLE_NOT_CARRIED` (target guards and a
  NOT EXISTS over same-job same-pid siblings; savepoint + exact RETURNING set per pid), run
  only after the deferred markers commit; the owner-only pass skips settled row ids; one user
  line from committed counts. owner_recovery.py: settled rows leave `_ELIGIBLE_ROW` (also
  covers rows the mailing sweep settled under #565).
- Tests: tests/test_king_live_job_not_carried.py, 20 cases; the acceptance case runs a whole
  King tax job with the real page pass. Positive control: the job-level and owner tests fail
  on the unfixed code.
- Not tested directly: the "deferred-marker commit failed, so no settlement" branch (a flag
  read by code inspection; Codex confirmed it).
- [ ] 2. Helper + SQL + `_fill_king_mailing_from_extract` out-param, helper tests.
- [ ] 3. Wire into the King block (label collection, swap after markers, counts, owner pass), job test.
- [ ] 4. ruff, OpenAPI, import, related suites. Codex diff review to GO. Ship (owner approved).
