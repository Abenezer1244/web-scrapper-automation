# King "no record" follow-ups to #565

Branch `fix/king-no-record-followups` (off main). #565 made the MAILING sweep settle a King
PIN as `parcel_not_found` when three King sources agree it does not exist. Three follow-ups:

## A. Live jobs settle at enrich time (enrich.py)

Today a live job's `no_record` parcel is deferred for mailing (`mailing_lookup_deferred`) and,
if it has no property address, for property (`property_lookup_deferred`); the mailing sweep then
spends a page request on its first tick to settle it, and the property sweep spends up to 5.

Plan:
- `_fill_king_mailing_from_extract(pid_map, job_id, answers_out=None)`: also hand back the
  extract answers it already read (keyed by lookup PIN), so the absence proof costs no second
  15 s scan. `None` out when the extract is unusable (then nothing can settle).
- `_apply_king`: collect pids whose phase-1 data has `parcel_lookup == "no_record"`.
- After the chunk driver, before the deferred markers: if any, AND the extract answers exist,
  AND `_king_left() >= 45`, run the shared proof (C). Only pids whose every row's lookup PIN is
  the printed pid (no account-number resolution) are eligible.
- Settled pids: removed from `deferred`; each row without a mailing address gets exactly what
  the sweep writes for this outcome (`mailing_recovery_outcome=parcel_not_found`,
  `mailing_lookup_deferred=false`, `mailing_source`, `mailing_rpacct_snapshot`) and
  `property_lookup_outcome=parcel_not_found` so `_mark_king_property_deferred` skips it.
  Anything unproven stays deferred exactly as today (the sweep still settles it later).

## B. Property recovery settles too (property_recovery.py + enrich.py constants)

Today `lookup == "no_record"` falls to "transient": charged, `gave_up` after 5 ticks (5 King
requests per dead PIN, and the honest outcome is lost).
- In the page loop, for requested pins with `parcel_lookup == "no_record"`: read the RPAcct
  extract for those pins only (`resolve_pins`; this sweep only reads the condo extract today),
  and if the tick has >= 45 s left, run the shared proof. Proven: `_write(..., "parcel_not_found")`,
  settled like `no_site_address`/`parcel_mismatch` (deferred false, outcome kept). Else unchanged.
- `PROPERTY_SETTLED_OUTCOMES` and `_SQL_MARK_PROPERTY_DEFERRED`'s NOT IN list gain
  `parcel_not_found`.

## C. One proof, one place (king_county_assessor.py)

`confirm_not_carried(pins, extract_answers) -> set[str]`: the pins the loaded extract answers
`absent` AND `king_gis_absent` returns. Callers own the page proof (the `no_record` label) and
the deadline. mailing_recovery `_settle_no_record` is refactored onto it (behaviour unchanged).

## D. Sweep stats label (mailing_recovery.py)

`stats["unreached"] = len(parcels) - len(attempted)` counts parcels that WERE fetched and settled
(mismatch, parcel_not_found) as unreached. Recompute it after settling as the parcels that were
neither mailing-attempted nor settled this tick.

## Out of scope

Owner recovery: charges every attempt and stops at 5 (owner_recovery.py:173), so bounded.

## Codex consult (2026-10-31): NO-GO as written; do C + D, then B, reassess A

- A deferred (owner's call). Its gain is one sweep request per dead PIN (the mailing sweep
  settles it on its next tick); its risk is in the central live-job transaction flow. Codex's
  minimum for A: conservative identity (exclude ANY resolved identity, exact 10-digit PIN,
  condo units by unit PIN), durable fallback markers per chunk then an atomic swap to settled,
  GIS bounded per 50-PIN batch inside the existing budget, guarded JSON writes, and owner-only
  pass suppression.
- C: at most one GIS request per proof (<= 50 PINs, county_gis chunk size); fail-closed;
  snapshot provenance; time-limit exceptions propagate.
- B: one RPAcct scan per tick (not per PIN), time checked before the scan (>= 60 s: ~15 s scan
  + 45 s GIS) and again before GIS (>= 45 s); `_write` treats `parcel_not_found` as settled;
  stats counter initialised; settling often skips after a long page pass, which is safe.
- D: "unreached" = this tick's page-work parcels neither mailing-attempted nor settled
  (mismatch or parcel_not_found). Mismatch had the same mislabel.

## Steps
- [x] 1. C (helper + mailing sweep refactor) and D, with tests.
- [x] 2. B, with tests.
- [x] 3. ruff, OpenAPI, app import, the related suites + new tests. Codex diff review to GO.

## Review (2026-10-31)

- C: `confirm_not_carried` + `NOT_CARRIED_SOURCE` in king_county_assessor.py (extract `absent`,
  first 50 only, one GIS request); mailing `_settle_no_record` uses it.
- D: mailing `stats["unreached"]` = parcels neither mailing-attempted nor settled, computed once
  after settling.
- B: property_recovery `_prove_not_carried` (once per tick, >= 60 s before the extract read,
  >= 45 s before GIS) settles `parcel_not_found` with provenance; `_write` settles every
  `PROPERTY_SETTLED_OUTCOMES` value; enrich.py's settled tuple and re-mark SQL guard include it.
- Codex diff review NO-GO -> fixed: [High] `resolve_pins` could DOWNLOAD the 18 MB extract
  (300 s timeout) inside the property tick; now `resolve_pins(..., refresh=False)` /
  `cached_extract(refresh=False)` reads the worker cache only (fresh < 24 h) and proves nothing
  otherwise. [Medium] tests now run the real cache code (only the download substituted, asserted
  never called) and the real page pass end to end. Re-review GO.
- Tests: 606 passed across the 31 related files + the new one. Positive controls: the new
  behaviour tests fail on the unfixed code; reverting `refresh=False` fails the cold/stale tests.
- Test-env gotcha: `src.db.session` rewrites the sync URL to :6543; locally nothing listens there
  unless a forwarder runs (a session-local asyncio forwarder 6543 -> 5432 was used). Without it,
  every sync-session test fails with "connection refused", which looks like a regression and is not.
- A (live-job settling) not built: owner's decision, see above.
