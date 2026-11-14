# Clark County, WA — seven record types (branch `feat/clark-wa-seven-record-types`)

Worktree `C:/Users/micha/bl-wt/clark-7`, based on origin/main `d7188fe2`. Research 2026-11-14:
read-only production SQL (Supabase `xqbrqvodxbursjjjlmjn`), public county pages, and bounded
read-only searches on the Clark recorder (no logins, no captchas, nothing purchased, no Tracerfy).

## Baseline (production, read-only, 2026-11-14; audit period = all time, 2026-09-10 to 2026-10-08)

Connectors: `70bb5315` recorder `clark_wa.ClarkWAScraper` [probate, pre_foreclosure] active/healthy;
`8037af43` `trustee_sale.ClarkWATrusteeSaleScraper` active/healthy; `99d8ca1b` dead inactive
`king_wa_probate.ClarkWaProbateScraper` (probate, template mode).

| type | jobs | done | failed | records_found NULL | found | saved/billed | already delivered | no owner | no parcel | no property addr | no mailing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| probate | 6 | 6 | 0 | 2 (both 2026-09-10) | 3,195 | 2,275 | 789 | 0/3,467 | 0/3,467 | 92/3,467 | 41/3,467 |
| pre_foreclosure | 1 | 1 | 0 | 1 (2026-09-10) | n/a | 25 | n/a | 0/27 | 0/27 | 0/27 | 0/27 |
| trustee_sale | 1 | 1 | 0 | 1 (2026-09-10) | n/a | 0 | n/a | – | – | – | – |

The three NULL `records_found` jobs all ran 2026-09-10 with every `breakdown_*` column NULL too
(predates those columns; not a live defect). Probate rows by doc type: TRANSFER ON DEATH DEED 1,911
(living owner, gated centrally by `include_living_owner_tod`), LACK OF PROBATE AFFIDAVIT 1,150,
DEATH CERTIFICATE 405, WILL 1. 1,128 of 3,467 rows `is_duplicate`.

## Trustee sale root cause (verified)

- `nts_notices` holds 2 Clark rows ever (last insert 2026-07-28), 0 active.
- The Columbian legal-notices listing, live 2026-11-14: 21 ads = 11 other, 6 probate, 4 summons,
  **0 trustee sales**. The crawler, parser and fetch all work; the source carries ~no Clark NTS.
- The crawler records a heartbeat (`nts_crawl:clark`, last 2026-11-13) whenever it reads the
  listing, so `trustee_sale` sees a "fresh" cache and returns 0 leads as if it were a real zero.
- The county recorder holds them: 58 NOTICE OF TRUSTEE SALE recorded 09/15-11/13/2026, each with
  debtor, trustee, PID, deed-of-trust refs, and a free "Unofficial Copy" page image
  (`/LandmarkWeb/Document/GetDocumentImage`). Raster, so sale date / principal owing need OCR.

## Defects found in existing recorder scraper (`clark_wa.py`)

1. A chunk that throws is logged and skipped (`except Exception: continue`); the job still ends
   DONE, silently missing that date window. Must fail loud (`ScraperExecutionError`).
2. Results wait timeout falls through to a 15 s sleep, then extracts; an unrendered page reads as 0.
3. Extractor picks "table with most rows" = the `#tableWrapper` wrapper, parsing one junk row per
   page. No reconciliation against the portal's own `#idTotCount`.

## Seven-type matrix (proposed)

| type | source | decision |
|---|---|---|
| probate | Recorder (DEATH CERT, LACK OF PROBATE, TOD, WILL) | keep; fix defects 1-3 |
| pre_foreclosure | Recorder (NTS, LP, NOTDEF, NF, FORECL, TRSL) | keep; fix defects 1-3 |
| trustee_sale | Recorder NTS images + OCR | Columbian source is barren. Needs OCR + review path |
| death_certificate | Recorder DEATH CERTIFICATE (code 62), recorded copies only | build as its own type; connector flip owner-gated |
| tax_delinquent | No county list; Bid4Assets seasonal list; balances only on 3rd-party pay site | blocked by source access |
| code_violation | County ArcGIS "Active Code Enforcement Cases" (652, no dates/owner, metadata says no bulk dump) | blocked pending written county permission |
| divorce | Odyssey index; AOC policy bars commercial solicitation use | blocked by legal/data restrictions |

## Codex consult (Review A, 2026-11-14) — reconciled, all adopted
1. High: silent loss also in `_search_chunk` render timeout and `_go_next_page` click errors, the
   50-page cap, empty/repeated page. Fix: raise `ScraperExecutionError`/`TransientScrapeError`
   (keep Celery time limits), count a chunk done only after validation, split windows over cap.
2. High: reconcile UNIQUE document ids across pages vs `#idTotCount` BEFORE filtering; account
   for PID/doctype/party/cancel exclusions separately. Read rows from `#resultsTable` directly.
3. High: results readiness must be tied to the submitted window/page (stale table risk).
4. High: hold Clark trustee_sale now with an explicit Clark availability gate; heartbeat alone
   cannot certify an empty deliverable cache (heartbeat + max(fetched_at) incl. inactive history).
5. High: reuse Chelan OCR + operator-review design; do not enable Clark against the unmerged branch.
   Needs discontinuance/cancellation + postponement policy.
6. High: cross-type billing dedup relies on parcel/address signature; test NTS pre_foreclosure ->
   trustee_sale identity; never confuse DT ref number with the notice's own instrument.
7. High: death_certificate needs its own checkbox + label maps and `orient_probate_party()` guard.
8. Medium: keep raw PID strings, bounded regexes; code_violation stays blocked; historical NULL
   metrics are unknown, not verified zero.

## Owner decisions (2026-11-14)
- trustee_sale: hold now (explicit Clark gate, fail clearly, never fake zero); Clark OCR adapter on
  Chelan's pipeline after it merges.
- death_certificate: build from recorder DEATH CERTIFICATE, connector ships inactive.
- tax_delinquent, code_violation, divorce: report blocked, build nothing.
- Phase 1 (recorder reliability): proceed.

## Todo
- [x] Codex consult (Review A) folded in
- [x] Owner check-in on plan + decisions
- [x] Phase 1: recorder reliability (defects 1-3) + tests. Codex gate: FAIL (High: header
  fallback could certify a short read; Medium: any empty cell accepted as zero), both fixed,
  re-review PASS. Live read-only 2026-11-14: probate Q1 288 docs -> 281 leads; explicit zero day;
  729-row window over 2 pages reconciled; pre_foreclosure 60 d 74 leads (58 NTS, 16 LP).
  Open finding: recorder PIDs are 6/7/8/9/10 digits (9 = PAN); not normalized without evidence.
- [x] Phase 2: Clark trustee_sale hold (`SourceUnavailableError`, fixed customer text, permanent,
  never billed). Codex: FAIL (message never reached users; test fixture leaked rows), both fixed,
  re-review PASS. Worker-level test needs the real test DB (CI only).
- [x] Phase 3: death_certificate (code 62, decedent oriented, PID required). Live 60 d: 21 docs ->
  18 leads. No connector change (ships dark). Codex: no findings.
- [x] Final adversarial review (Review C): NO-GO with 4 findings, resolved below.
- [x] Owner approved push / deploy / turn on (2026-11-14). PR #631 merged (main 92db4278), Railway
  auto-deployed api/worker/beat; death_certificate added to connector 70bb5315.
- [x] Go-live verification, browser QA and parcel-id audit (below).

## Review C (final adversarial, 2026-11-14) — resolution
1. High "a server cap fails busy windows permanently": REFUTED by measurement. A 2023-01-01..
   2026-10-31 probate window (Show Count 3,885) read completely over 8 pages of 500 and
   reconciled; header then reads "Returned 500 records of" (blank), which resolve_total already
   refuses. Clark 30-day windows hold ~150. No bisection code added.
2. Medium "catch-alls swallow Celery time limits": FIXED, `reraise_time_limit` in all three handlers.
3. Medium "no real-portal contract test": PARTIAL. Added row-id format check + test; DOM path is
   verified by the live runs above, not by a committed HTML fixture (real rows carry names).
4. Medium rollout: production has ONE Clark trustee_sale config, manual (no schedule), so no
   repeated failure notices. Canary will mark connector 8037af43 'down' once (one ops alert) and
   hide Auction leads from the Clark picker: expected for the hold.

## Seven-type matrix (2026-11-14)

| type | implementation | source | status |
|---|---|---|---|
| probate | existing, repaired | Auditor recorder LandmarkWeb (free, no login) | Live, verified on prod worker |
| pre_foreclosure | existing, repaired | same (NTS, LP, NOTDEF, NF, FORECL, TRSL) | Live, verified on prod worker (11/01-11/13: 15 leads) |
| trustee_sale | existing, held | Columbian classifieds (barren); recorder images need OCR | Blocked by source access (hold), OCR later |
| death_certificate | new | recorder DEATH CERTIFICATE (recorded copies) | Live 2026-11-14, real job verified end to end |
| tax_delinquent | none | no delinquency list; Bid4Assets seasonal; paydici balances | Blocked by source access |
| code_violation | none | county ArcGIS layer, "not subject to public disclosure, no dump" | Blocked by legal/data restrictions |
| divorce | none | Odyssey index; AOC policy bars commercial solicitation | Blocked by legal/data restrictions |

## Activation prerequisites (owner approval for each)
1. PR + CI green (CI runs the DB-backed worker test).
2. Deploy api, then worker, then beat (no migration in this branch).
3. Verify: one Clark probate and one pre_foreclosure manual run finish DONE with records_found set.
4. death_certificate: `UPDATE county_connectors SET record_types = record_types || '["death_certificate"]'
   WHERE id = '70bb5315-1cec-428b-9f7d-b5058293083e';` (or a migration) only after approval.
5. Clark OCR trustee_sale: after feat/chelan-nts-ocr merges.

## Go-live verification (2026-11-14)
- Non-persisting run on the prod worker: death_certificate 10/15-11/13 6 leads, pre_foreclosure
  11/01-11/13 15 leads, trustee_sale raises SourceUnavailableError with the customer text.
- Real death_certificate job fe8f35b3 (owner account b6d2095d, 08/15-11/13, contact lookup off):
  DONE in 3 min; 32 found, 24 already delivered (earlier probate runs), 8 new billed; all 8 with
  owner, parcel, property and mailing address; 0 degradations. CSV export: 8 rows, every core column
  8/8, phone/email empty (lookup off).
- Real Auction-leads run ac972e54 from the UI "Run now": failed, retry 0, billed 0, reserved 0, the
  exact customer text in the run log and the "This run didn't finish" panel.

## Browser QA (Playwright Chromium, owner signed in; 2026-11-14)
Pages: run results (fe8f35b3), scraper list, dashboard, new-scraper wizard with Clark selected.
Widths 320, 375, 390, 430, 768, 1024, 1440, 1920, light and dark: 64 of 64 with no horizontal
overflow and no console errors. Clark shows 4 types incl. Death Certificate (Auction Leads still
listed until the canary samples it). Notes for the frontend: the sidebar "Today - Running" dot
stayed on after the Auction-leads run failed; the failed-run card has a large empty area.
Data note: "Heirs" on a Clark death certificate is the person indexed beside PUBLIC (likely the
filer/spouse); the role is inferred, not stated by the recorder (same as probate). "Current
Owner" / "Title Status" stay empty for Clark (that reconciliation is King-only).

## Parcel-id audit (2026-11-14, branch fix/clark-parcel-ids)
3,526 Clark leads by PID length: 6:4, 7:32 (4 no mailing), 8:816 (14), 9:2,659 (8), 10:14 (14),
12:1 (1). The county parcel layer (TaxlotsPublic Prop_id) accepts 8 and 9 digits as printed, so no
leading zeros are lost. 10/12-digit and some 7-digit ids are recorder typos (1428590000 is
142859000; 9200786 is 92007860; 1181107374 resolves neither way; one legal says "INCORRECT
PARCEL"). They get no address and are not billed. Fix: only an explicit "PID:" is a parcel (the
unlabelled fallback fired once, on a BLK number), the whole digit run is kept verbatim, and all
PIDs of a multi-parcel deed (140 rows) go to enrichment_data["parcel_ids"]. No repair of typos:
guessing a zero could pick another owner's parcel. Codex consult + gate PASS; live lead counts
unchanged (281 / 711 / 74 / 0). Merge only while no Clark job runs (enrichment_data is part of the
in-job row fingerprint).

## Review
Three defect families fixed in the live Clark recorder scraper, Clark Auction leads put on an
honest hold, a dark death_certificate type added, three types documented as blocked with
evidence. Live and verified 2026-11-14 (above). Still open: OCR trustee_sale (after
feat/chelan-nts-ocr), the three blocked types, and the "Heirs" role inference.
