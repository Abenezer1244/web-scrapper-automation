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
- [ ] Phase 2: trustee_sale per owner decision
- [ ] Phase 3: death_certificate type
- [ ] Per-type Codex reviews, final adversarial review, report
