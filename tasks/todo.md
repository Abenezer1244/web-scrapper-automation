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
