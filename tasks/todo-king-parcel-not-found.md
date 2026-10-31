# King mailing recovery: settle parcels eRealProperty proves do not exist

Branch `fix/king-parcel-not-found` (off main `077f5959`).

## Problem (verified 2026-10-31, Codex agreed: Medium)

`recover_deferred_mailing` re-fetches the same 4 King parcels (10 lead rows) every
~10 minutes, forever. Each comes back "HTTP 200, no parcel echo" (`HTTP200_no_parcel`).
Phase 1 treats that as a failure; the sweep charges only mailing attempts, so these are
rotated uncharged (`mailing_recovery.py:341-358`). The comment there relies on the phase-1
breaker stopping a persistent failure, but the breaker needs 30 failures
(`king_county_assessor.py:63`) and the whole backlog is 4 parcels. `_MAX_ATTEMPTS` is never
reached. Cost: ~576 needless eRealProperty requests/day; 10 leads stuck "deferred" with no
honest outcome. It breaks the rule written at `county_gis.py:705` (a fetched and judged page
spends an attempt "instead of being fetched again every tick forever").

Reproduced live with the worker's URL and User-Agent: a real PIN echoes itself; a
nonexistent PIN returns 200 with `<span id="cphContent_LblMsg">No data found.</span>`, an
empty PARCEL section (no `cphContent_DetailsViewDashboardHeader` table) and no echo.

## Design (Codex consult 1: strengthened Option A)

Settle as the EXISTING terminal outcome `parcel_not_found` only on three independent proofs,
all from this tick:
1. eRealProperty page: no parcel echo AND the explicit "No data found." message
   (`cphContent_LblMsg`) AND no `DetailsViewDashboardHeader` table.
2. RPAcct extract loaded successfully (`resolve_pins` not None) and says `absent` for it.
3. King GIS parcel layer answered (not in `county_unreached`) and has no feature for it
   (exact `PIN IN (...)`; King skips the statewide fallback; the kill switch strips only
   mailing fields, never the PIN lookup).
Any other combination: unchanged (uncharged rotation).

Rejected: charging an attempt for every no-echo page (Option B). A 50-minute interstitial
on a small backlog would terminalise valid parcels without the breaker seeing it.

## Steps

- [x] 1. `king_county_assessor.py`: `_is_no_record_page(html)`. In phase 1, a no-echo page
      that matches it gets `results[pid] = {all None, "parcel_lookup": "no_record"}`. It is
      still a ledger failure (breaker unchanged) and still deferred (live jobs unchanged:
      `enrich.py` reads only verified/mismatch/recovered, `property_recovery.py` treats it
      as transient, charged and bounded).
- [x] 2. `mailing_recovery.py` `_recover_impl`: keep the extract answers; after phase 1,
      for un-attempted parcels with `parcel_lookup == "no_record"` and extract `absent`,
      ask King GIS once (batched); settle the ones GIS also lacks through `_apply` as
      `mailing_lookup = parcel_not_found` with source provenance; rotate the rest as before.
- [x] 3. Tests: classifier on the real "No data found." page shape vs real/vacant/
      interstitial pages; sweep settles only when all three proofs hold (each proof
      missing or erroring -> rotated, uncharged); GIS unreached -> rotated; extract None ->
      rotated; settled rows are terminal and leave the candidate set.
- [x] 4. ruff, OpenAPI check, app import, targeted pytest. Codex review to GO.

## Review (2026-10-31)

- `king_county_assessor.py`: `is_no_record_page` (exact `cphContent_LblMsg` "No data found."
  + no DashboardHeader + no echo); phase 1 labels it `no_record` (ledger and deferral
  unchanged); `king_gis_absent` (answered-and-missing only; empty set without the King
  layer config).
- `mailing_recovery.py`: `_settle_no_record` (three proofs, GIS only with >= 45 s of the tick
  left), settled through `_apply` as `parcel_not_found` with source
  `king_erealproperty+king_gis+king_rpacct` and the extract snapshot; rotation excludes them.
- Tests: `tests/test_king_no_record_page.py` (classifier on verbatim live slices, phase-1
  label), `tests/test_mailing_recovery_no_record.py` (settles only on all three; extract
  missing/listing the PIN, GIS street-less match, 503, error body, connection error, capped
  page, no label, no time left all rotate uncharged; one end-to-end run of the real phase 1).
  Positive control: 13 of the first 19 fail on the unfixed code. 30 related files: 595 passed.
- Codex: consult GO (4 findings folded in), diff review GO (2 Low test gaps, both closed),
  re-review GO.
- Gotcha logged: a `sed 's#\([^ ]*\)#tests/\1#g'` file list also matches the empty string
  after a trailing space and yields a bare `tests/`, which runs the whole suite; and two
  pytest runs against one test DB collide (fixture DELETEs).
- Not verified: the 4 production PINs (production reads are blocked for Claude). After
  deploy, the next tick's log should read "settled as parcel_not_found" or keep rotating
  if any proof fails; either outcome is safe.

## Out of scope (follow-ups)

- Live jobs could settle such a parcel at enrich time instead of on the first sweep tick.
- `property_recovery` could settle `parcel_not_found` instead of spending 5 transient attempts.
- F2 (session advisory lock under transaction pooling) and F3 (per-replica extract cache): Low.
