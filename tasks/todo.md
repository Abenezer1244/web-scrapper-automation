# BridgeLeads — Pierce County Fix Plan

**Date:** 2026-03-26
**Goal:** Fix Pierce County scraper to reliably extract parcel IDs, property + mailing addresses

---

## Phase 1: Database Cleanup
- [x] 1.1 Delete garbage records (bad dates, HTML fragments)
- [x] 1.2 Mark 2 stuck "scraping" jobs as "failed"
- [x] 1.3 Fix connector base_url to correct ARMS path
- [x] 1.4 Remove dead `_fetch_parcel_ids` method and `_run_enrichment` from tasks.py

## Phase 2: Fix Pierce Scraper
- [x] 2.1 Fix date inputs — use keyboard typing for Infragistics WebDateChooser (JS value= doesn't register)
- [x] 2.2 Fix PROBATE filter — Playwright click on checkbox (now returns 88 records, not 300)
- [x] 2.3 Fix pagination — use `#OptionsBar1_imgNext` arrows with last-page detection via page dropdown
- [x] 2.4 Replace one-by-one enrichment with batch GIS (50 parcels/call)
- [x] 2.5 Harden date validation (reject instrument numbers as dates)
- [x] 2.6 Remove `--single-process` Chromium flag (caused crashes)
- [x] 2.7 Crash-safe browser cleanup in `__aexit__`
- [ ] 2.8 Fix parcel ID extraction from detail page dropdown (currently 7/88 = 8%)
- [ ] 2.9 Test full pipeline end-to-end

## Phase 3: Enrichment
- [x] 3.1 GIS batch enrichment works (verified: parcel 3887100470 → 19123 11TH AVENUE CT E)
- [ ] 3.2 Improve parcel-to-address match rate (GIS found 1/7 = 14%)

---

## Current State
- 88 PROBATE records scraped correctly (01/01/2026 - 03/26/2026)
- 4-page pagination works (25+25+25+13)
- 7 parcel IDs from inline legal description, 1 GIS-enriched address
- Remaining: detail page dropdown iteration not matching instruments → parcels
- Root cause: `document.querySelector('select')` picks page dropdown instead of instrument dropdown on detail page

## Review
*(To be filled after completion)*
