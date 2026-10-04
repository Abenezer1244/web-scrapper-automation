# Mailing follow-ups (2026-10-03)

Branch `fix/mailing-followups-2026-10-03` (from origin/main `9933c203`), worktree `C:/Users/Windows/bl-wt/mailing-audit`.
Owner: "Complete these" (5 follow-ups from the 10-03 backfill session).

## Evidence gathered (read-only, prod via `railway ssh --service worker`)
- **TaxSifter owner fill** (`taxsifter.fill_addresses_by_owner`, Okanogan + Douglas only): writes addresses + `assessed_value`, no `mailing_source`; flags stay NULL because the TaxSifter situs is street-only, so `property_state` is never set (`address_intel.compute_owner_flags` needs it for out_of_state and for absentee when streets match).
- **King code_violation `37014cb9`** (1,756 rows, 635 mailing): the Seattle SDCI parcel-locate step (`enrich.py:1249+`, 420 s budget) reached only ~308 rows (357 `address_mismatch`, ~812 never reached). Unreached rows are only ever revisited by a MANUAL script (`scripts/backfill_king_code_violation_mailing.py`); there is a beat sweep for CV owner NAMES (`cv_owner_recovery`) but none for CV mailing. Root cause = no second look, not a source outage.
- **Pierce**: 200 done rows without mailing, not 96: 96 have a parcel but no deferral marker and no recovery outcome (never retried, jobs since 06-23); 104 have no parcel (cannot be looked up). 4 parcels are 9 digits (Pierce = 10).
- **Kitsap / Whitman / Douglas**: prod has NO Kitsap or Douglas configs, and Whitman's single probate config has never finished a job, so there are zero rows to fill. Legal: Kitsap "No one is permitted to sell this information except in accordance with a written agreement with Kitsap County" (RCW 42.56.070(9)); Whitman GIS "cannot be ... used to generate commercial mailing lists"; Douglas hub "requires permission".
- **Okanogan**: 43 rows, 0 parcels. No verified parcel source for a name-only probate lead (bulk `parcels.zip` 404; recorder index has no parcel field). "Never a parcel from a name" stands.

## Phase 1 — code (≤5 files)
- [ ] 1a. `taxsifter.fill_addresses_by_owner`: stamp `mailing_source=taxsifter_<county>` when it fills mailing, `property_source=taxsifter_<county>` when it fills the situs; set `property_state='WA'` ONLY when this helper filled the situs and the row has no state (never overwrite; the TaxSifter site only lists its own WA county). Flags then come from the existing end-of-job recompute. Tests: provenance stamped / not stamped on a partial fill, out-of-state mailing + WA situs → out_of_state_owner True, existing state untouched.
- [ ] 1b. King CV mailing recovery beat sweep: the "second look" for rows the job's 420 s budget never reached, reusing `king_parcel_locate.resolve_code_violation_mailing` and the guarded write already in `scripts/backfill_king_code_violation_mailing.py` (move that write into the worker module; script calls it). Bounded per tick, shared King lease + the existing `source_health` cooldown on 403/429 (same pattern as `cv_owner_recovery`), never parcel_id/dedup/billing. Eligibility = terminal job, no parcel_id, no mailing, no `kc_pin_status` (the step stamps every row it decides), so a decided row is never re-asked. Per-tick log: attempted / found / blocked / remaining. Semantics match the existing mailing recovery: a done job's enrichment may continue in the background. + beat entry + tests (lease held elsewhere, cooldown, guarded no-op write, repeat tick).
- [ ] Verify: targeted pytest on the isolated test DB, Codex review, CI.

## Phase 2 — ops on prod (owner approval per item, dry run first)
- [ ] 2a. Pierce: `scripts/requeue_gis_mailing_recovery.py --counties pierce` (DB-only markers) → the existing paced recovery sweep looks them up. Targets only the 96 rows WITH a parcel (the 104 without stay excluded). The 4 nine-digit parcels: check Pierce GIS read-only; leave them to the sweep's own parcel_not_found outcome, never zero-pad.
- [ ] 2b. King CV: let the new sweep drain `37014cb9` (or run the existing backfill with `--limit`, paced 0.35 s; King has rate-blocked us twice).

- [ ] 2c. Stamp the 1 historical TaxSifter-filled Okanogan row (guarded single UPDATE: provenance + property_state + recomputed flags).

## Not doing (owner to confirm)
- Kitsap / Whitman / Douglas sources: zero leads today + license terms that need an owner/legal decision. Revisit when a customer runs one of them. Whitman's only config is the owner's admin config from 10-02 whose one job was CANCELLED that day (not a failing source), so no gating change (Codex P1 disputed with this evidence).
- Okanogan parcel source: none exists for name-only leads. Owner option: request the county's parcel file (Okanogan GIS, (509) 422-7123; FTP okgis.ddns.net). Until then 1a is the only automatic path. A county file only helps if it links owners to parcels AND its terms allow commercial use.


# Mailing-address forensic audit (2026-10-02)

Branch `audit/mailing-address-2026-10-02`, worktree `C:/Users/Windows/bl-wt/mailing-audit` (cut from origin/main `d10f95a6`; the main checkout is detached 562 commits behind, do not audit it).

Owner's question: for every supported WA county and record type, does BridgeLeads retrieve a legitimate owner/taxpayer mailing address when one is available? Evidence, not assumptions.

## Phases
- [x] 0. Fresh worktree from origin/main; read prior mailing history (memory + `docs/HANDOFF-mailing-address-2026-09-13.md`)
- [x] 1. Map architecture source -> UI (5 parallel read-only mappers: county_gis, enrich orchestration, persistence/API/export, scraper registry, recovery sweep)
- [x] 12. Read-only prod DB analysis (`scratchpad/dq_mailing_audit.py` via `railway run`, role postgres, transaction_read_only=on). Result: coverage is COUNTY-shaped, not record-type-shaped.
- [x] 4. Clark + Benton probate jobs pulled (ids, ranges, counts, logs, samples) — see report
- [x] 2/3. County x record-type matrices (code + prod config + official-source research) — see final report
- [x] 5. Real parcels verified: Clark 196948000 + 97976264 (PIC), Benton 131073011125003 (PACS: county publishes mailing, BridgeLeads NULL), Okanogan 9935250696 / 1250110000 / 3322070010 (TaxSifter: 1250110000 is county-owned, assigned to two leads = wrong inference), Thurston 74700001201 (A+)
- [x] 6/7/9/10/11. Confirmed from code: no property->mailing copy in enrichment (but the PACS grid parser WAS a situs echo: fixed); parcels never int-cast; skip trace gates only Tracerfy; dedup does not block later lookups; one column name end to end
- [x] 8. Sources probed: PACS forms identical on 6 portals (Island offline), Clark PIC 429 after ~10 fast requests, Chelan detail GET 500 without session, Whitman token-required, Kitsap psearch 500s
- [x] Phase 1 `86195315`: pacs_parcel.py adapter (7 PACS counties) + pacs.py situs-echo fix + detail parser; 72 tests
- [x] Phase 2 `c07d7c78`: thurston_assessor.py; 24 tests
- [x] Phase 3 `1d2c3d2a`: Okanogan never infers parcel from surname; TaxSifter owner addresses shared helper; 36 tests
- [x] Phase 4 `77a6a86c` + `e4f1d153`: workers/data_quality.py hourly sweep + ops alert + scripts/data_quality_report.py; 17 tests. Prod dry run: king code_violation 37014cb9 is a TRUE positive (3.5% mailing vs 65% baseline)
- [x] Phase 5 `a91da0db` (stacked on -76's 9ff6bceb): wire adapters into county_gis `_BULK_MAILING_SOURCES`; `has_mailing_source()` drives deferral + `mailing_missing`; recovery/backfill discover `parcel + NULL mailing` rows marker-independently (Snohomish tax 1,751)
- [x] Phase 6 `99673842`: backfill script (dry-run default, county-scoped, fill-only, outcome counts) — counts to owner, approval before apply
- [x] Phase 7: live E2E (pipeline -> DB -> API -> CSV): Benton 4/4 MATCH vs independent county read, Thurston filled, Clark deferred (PIC URL serves no record; reported to -76). Codex r1-r5, r5 GATE: PASS. Journal entry. UI rendering verified by code only (MailingValue renders the API value verbatim); no browser run
- [x] Codex consult with the evidence (scratchpad `codex_consult_out.txt`): root cause CONFIRMED fleet-wide; P1s to honor: PACS parcel→prop_id identity must be corroborated (explicit parcel_mismatch), never reuse the owner-NAME PACS parser for parcel enrichment, PACS "mailing" built from address lines may be a situs echo (reject mailing == property unless the page labels it mailing), provenance on every write, backfill discovers rows independent of deferred markers with dry-run + per-source outcome counts + rollback; DQ report needs outcome distributions not just coverage %.
- [x] Coordination: Clark PIC mailing is being built by session -76 (worktree wt-clark-mailing, uncommitted, Codex P1s in progress, no PR). Clark is OUT of my fix scope; I stack on its `_BULK_MAILING_SOURCES` / `has_mailing_source()` after it merges. Until then I touch only NEW files.
- [ ] CHECK-IN with owner: findings + proposed fix scope before any code change
- [ ] 13. Admin data-quality health report (per-run coverage %, baseline comparison, warning)
- [ ] 14/15. Fix at the right abstraction (county adapter for the counties that have a public source); never fabricate; fill-only writes
- [ ] 16. Historical backlog counts (done, see report) + idempotent backfill design (no quota, no skip-trace, resumable, rate-limited) — approval before running
- [ ] 17. Regression tests (list in owner prompt)
- [ ] 18. Browser E2E (Playwright, not Claude-in-Chrome) for Clark + Benton probate
- [x] Codex review r1-r5 (r5 GATE: PASS); journal; memory

## Working notes
- Prod kill switch `COUNTY_GIS_RESTRICTED_MAILING_ENABLED=true` on worker (read 2026-10-02) — not the cause.
- Clark probate job `62404bd0` (admin, 09/01/2025..09/30/2026): 1335 rows, 1335 parcel, 1292 property, **0 mailing**, 0 deferred markers. Logs never mention a mailing lookup; "Enrichment complete: addresses added".
- Benton probate job `bc8d507c` (admin, 07/04..10/02/2026): 7 rows, 4 parcel, 4 property, 0 mailing.
- Every 0%-mailing county (benton, chelan, clark, okanogan) is 0% on EVERY record type it has; king/pierce/snohomish/cowlitz are 56–100%. Snohomish tax_delinquent since 09-18: 1,751 rows, 0% mailing, parcel 100%, no deferred marker (pre-#346 rows, never retried).

## Review
Root cause: county coverage gap in the shared enrichment (no mailing source outside pierce/king/snohomish/cowlitz), plus a PACS situs-echo parser and Okanogan surname-invented parcels. Fixed on this branch (11 commits on top of -76's Clark commit); 475 targeted tests green; full suite NOT run (memory-reaped). Not merged, not deployed, no prod data changed. Next: -76 PR, this PR, backfill dry runs, owner approval per county.
