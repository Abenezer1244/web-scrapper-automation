# Test 10 — King Tax Delinquency: root-cause investigation

Branch: `fix/test10-king-tax-dq` (worktree `C:/Users/Windows/bridgeleads-worktrees/test10-dq`, off `origin/main` @302edd5)

## Baseline (job 960abfdf-60c2-4f95-abb3-410a86634572, 384 rows, ran 2026-09-02 09:37 UTC)

| Field | Populated | Missing |
|---|---|---|
| Date | 384 | 0 (but every value is a fabricated `01/01/<bill_year>`) |
| Party Name | 0 | 384 |
| Parcel ID | 384 | 0 |
| Tax Balance Owed | 384 | 0 (100 are understated) |
| Oldest Tax Year | 384 | 0 (100 are wrong) |
| Property Address | 212 | 172 |
| Mailing Address | 212 | 172 (all 212 fabricated) |
| Phone | 0 | 384 |
| Email | 0 | 384 |

Job log shows the King enrichment died at the outer `asyncio.wait_for(240)`:
`09:38:10 Looking up 172 mailing addresses...` -> `09:42:11 Address enrichment failed`.

## Confirmed root causes (each verified against live source data)

- **RC-1 [P1] Party Name never populated.** Owner name IS available from eRealProperty
  (verified live: 6/6 sampled parcels returned a real owner). Two mutually-exclusive
  write paths: primary (`enrich.py:441`) requires `not mailing_address`; fallback
  (`enrich.py:596`) requires `mailing_address` AND caps at `_MAX_KING_OWNER_PARCELS = 25`.
  Codex flagged GIS-starvation of the primary path — TRUE at Test 10 time, but NOT on
  main (King GIS now has `mailing_fields: []`). Residual defect on main = the caps.
- **RC-2 [P1] Tax Balance Owed understated + Oldest Tax Year wrong.** The Socrata
  `$where` clips `bill_year` to the job's UI date window (`king_wa_tax_delinquent.py:471`)
  and the aggregator re-drops out-of-range years (`:265`). Contradicts the module's own
  contract (`:17`, "across all its delinquent years"). Live dataset spans 2002-2026;
  3,569/17,297 accounts have a pre-2025 delinquent year.
  **Test 10 impact: 100/384 leads (26%) wrong; $652,958.57 of delinquent tax omitted.**
- **RC-3 [P1] Date is fabricated.** `date_recorded = "01/01/<bill_year>"` (`:338`) is a
  synthesized event date; the tax roll has no filing/recording date. The FE already
  papers over it (`ResultsTable.tsx:222` renders an em-dash for tax rows), but the
  value is real in the DB and in the CSV export.
- **RC-4 [P1] Mailing Address fabricated (historical, already fixed on main).** Test 10's
  212 mailing addresses are the situs echoed with city/state/ZIP. **All 6 sampled are
  wrong vs source; 3 of 6 are absentee owners** whose real mailing is elsewhere
  (e.g. 0871001805 -> 737 OLIVE WAY UNIT 3503, SEATTLE, not the property).
- **RC-5 [P2] Silent cap gap.** `king_county_assessor.py:478` truncates to 200 mailing
  lookups and discards the overflow WITHOUT adding it to `st["deferred"]`, so those
  parcels get no durable marker (worse than Codex's report of the same cap).

## Safety checks done
- `dedup_hash` uses the strong branch `_legacy_strong_signature(parcel_id, property_address)`
  whenever parcel_id exists (always for King tax), so nulling `date_recorded` does NOT
  change billing/dedup identity. `_source_fingerprint` is per-job only.

## Plan
- [ ] P0. Fresh baseline run on UNCHANGED main to separate "already fixed" from "still broken"
- [ ] P1. RC-2 fix: aggregate amount + oldest year over ALL delinquent years; keep window as parcel SELECTION only
- [ ] P2. RC-3 fix: stop fabricating date_recorded for King tax
- [ ] P3. RC-1 fix: unify owner resolution, budget-based not arbitrary-25, circuit-breaker retained
- [ ] P4. RC-5 fix: record cap overflow in deferred
- [ ] P5. Regression tests from real source shapes
- [ ] P6. Codex review of the diff; independent verification
- [ ] P7. Live post-fix scrape + per-field verification against King source
