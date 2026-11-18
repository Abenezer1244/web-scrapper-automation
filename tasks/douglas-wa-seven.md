# Douglas County, WA — seven record types (branch `feat/douglas-wa-seven-record-types`, worktree `C:\Users\micha\bl-wt\douglas-7`, from origin/main c8fcfecb)

Phase 1 (research + audit) done 2026-11-15: read-only, no logins, nothing bought, nothing written to production.

## Baseline (production) — BLOCKED, owner action needed

Connector `douglas` (652ed7fd, AcclaimWeb template, `https://edocs.douglascountywa.gov/AcclaimWeb`): active, health `healthy`,
record_types `[probate, pre_foreclosure]`, `max_date_range_days` 30, `assessor_url` NULL, `gis_endpoint` NULL, last_checked 2026-11-15 14:18Z.

Measured 2026-11-15 (owner-approved, `DATABASE_URL_MIGRATE` in a READ ONLY transaction, aggregates only; the worker's
RLS-scoped `bridgeleads_app` role sees 0 rows everywhere, so it cannot baseline):

| Type | Jobs | Outcome | records_found | Results | Delivered/billed |
|---|---|---|---|---|---|
| pre_foreclosure | 3 (all manual, 2026-10-17, window 09/17–10/17) | 2 done, 1 failed (`records_found` NULL on the FAILED one, correct) | 6 + 6 | 12 (6 new + 6 already-delivered dups); 10 NTS, 2 LIS PENDENS | 6, one external customer, 3 active configs |
| probate | 0 | never run | - | 0 | 0 |

Field completeness: 12/12 party, parcel (all 11 digits), property address, mailing; 0/12 `date_recorded` (blank on the
Oct code path). No Douglas `nts_notices` / `county_records`. Done runs with NULL records_found: 0.

Key finding: all 12 stored parcels equal the grid `ParcelNumber` of their instrument (6/6 delivered, 6/6 dup), and every
delivered `dedup_hash` = `legacy_strong_signature(parcel, scrape-time address)` (3 with the TaxSifter situs, 3 blank).
The Nov 2026 grid rewrite stopped reading `ParcelNumber`, so main now hashes Douglas rows WITHOUT the parcel: an
overlapping re-run would re-deliver and re-bill those 6 leads. Reading it again restores the exact keys; no data repair.
Live check: the 3 TaxSifter-addressed delivered rows still return the same (page-echoed) parcel and the same address.

TOD flag (all counties, probate configs): false 5 (4 active), NULL legacy 3 (0 active, 1 user), true 68 (64 active,
6 users). Douglas: 0 probate configs. Living-owner TOD leakage therefore has no live exposure; the legacy rule is unchanged.

## Source verification (live, 2026-11-15)

Recorder doc-type list read live (306 types). 12-month counts, 11/15/2025–11/15/2026, one doc-type search each
("0" = no grid answer within 60 s, which is how this portal answers an empty search; treat as unverified zero):

| Code | Type | 12 mo | Grid ParcelNumber |
|---|---|---|---|
| NTS | NOTICE OF TRUSTEE SALE | 33 | 11 digits on all page rows |
| NDTS | NOTICE OF DISCONTINUANCE | 16 | 11 digits |
| NOCONT | NOTICE OF CONTINUANCE | 50 | 11 digits |
| ANTS / ND / NOF | AMENDED NTS / NOTICE OF DEFAULT / NOTICE OF FORECLOSURE | 0 / 0 / 0 | - |
| LP | LIS PENDENS | 3 | 11 digits |
| DC | DEATH CERTIFICATE | 21 | blank or 11 digits |
| AFFIDE | AFFIDAVIT OF DEATH | 2 | |
| LACKPRO | LACK OF PROBATE AFFIDAVIT | 50 | blank, 11, one 12-digit |
| WILL / LT / LA | WILL / LETTER OF TESTAMENTARY / LETTERS OF ADMIN | 1 / 0 / 0 | |
| TOD | TRANSFER ON DEATH DEED (living owner) | 70 | |
| TAXD | TAX DEED (after tax foreclosure) | 11 | |
| DISSOLUTION / REDIVORCE | DIVORCE / REAL ESTATE DIVORCE | 0 / 0 | - |
| VIOLATION | VIOLATION | 0 | - |

Grid row keys include `ParcelNumber`, `DocLegalDescription`, `TransactionItemId`, `NumberOfPages`. Recorder disclaimer: as-is,
no commercial-use or automation restriction text.

Other sources (research agent, read 2026-11-15; quotes in session notes):
- Treasurer: "2026 Notice and Summons" (scanned 11-page PDF, 2026-10-27, needs OCR) = current foreclosure-stage list;
  "Tax Title Properties" PDF; Bid4Assets auction. Tax-roll FTP export is "intended for ... tax services and mortgage
  companies", needs a Public Records Request on file; "delinquent files are available year round".
- TaxSifter: disclaimer has no commercial-use ban; no robots.txt (404); click-through gate; per-parcel tax fields not yet seen.
- GIS: `Parcels_view_(Joined)` FeatureServer (already the Douglas mailing source in `county_gis.py`), modified 2026-11-10, no license text.
- Code enforcement: county = SmartGov complaint intake only; no case-level data found; East Wenatchee = request tracker only;
  other cities not located. Recorder VIOLATION = 0 in 12 months.
- Courts: Douglas listed as an Odyssey Portal county (anonymous: case index only, no documents; terms not read).
  JIS-Link = paid subscription ($200 setup, $0.145/transaction). Recorder divorce types = 0 in 12 months.
- Death: certified copies restricted to qualified applicants (DOH/CDHD, $25 each); no bulk index of recent deaths.
  Recorder DC (21/yr) is the only feed, same as Chelan.

## Code audit — defects (verified against the live list)

| # | Defect | Evidence | Severity |
|---|---|---|---|
| G1 | Douglas grid `ParcelNumber` (11-digit, from the recording itself) is deliberately ignored; parcel comes only from an owner-name TaxSifter lookup (no parcel_id ever set) | `acclaimweb.py` `_grid_item` sets `"parcel": ""` | High: parcel/owner resolution rests on name matching |
| G2 | Generic map: `LIS PENDENS` matches Douglas `RELEASE LIS PENDENS`; guard only knows "RELEASE OF LIS PENDENS" | `preforeclosure._CANCELLATION_ADMIN` | Medium (cleared lis pendens sold as leads) |
| G3 | Probate keywords miss `AFFIT OF HEIRSHIP`, `LETTER OF TESTAMENTARY`; `DEATH` admits `AFFI DEATH JOINT TENANT` (survivorship, no estate) | live list vs `_DOC_TYPE_MAP` | Low-Medium |
| G4 | No Douglas `death_certificate` or `trustee_sale` vocabulary; connector types are only probate + pre_foreclosure | registry/connector | - |
| G5 | Douglas still uses dead tokens (`NOTICE OF DEFAULT`, `NTS`/`NOD` whole-word on text) | 0 recordings / text descriptions | Cosmetic |

Completeness (stale grid, pagination, "of N" check, single-date `date_to`) was fixed in the shared template during Chelan
(#625) and verified on Douglas live 2026-11-12.

### Codex pre-build consult (2026-11-15, read-only) — reconciled, all adopted
1. **Critical:** reading `ParcelNumber` changes billing `dedup_hash` (parcel|address, `property_identity.py:64`), same-job
   fingerprints (`tasks.py:1290`) and overlap keys. Needs an inventory of delivered Douglas rows and a key-preserving repair
   before rollout. Phase 2 is gated on the baseline.
2. **High:** the TaxSifter owner-name lookup fills situs/mailing for every row without checking the recording's parcel
   (`taxsifter.py:297`); a unique name match can be another property. Fix: parcel-keyed enrichment when the recording names a
   parcel; owner-name fallback only for rows proven to have none (Chelan's found/ambiguous/none outcomes).
3. **High:** G2 confirmed. NDTS is already dropped (`DISCONTINU`); NOCONT matches nothing. In trustee_sale, NDTS = cancel,
   NOCONT = sale-date update, never a fresh lead.
4. **High:** TOD leakage: legacy configs (`include_living_owner_tod` NULL) receive living-owner TOD deeds (70/yr in Douglas).
   Audit Douglas config flags in the baseline. LACKPRO = nonprobate transfer signal, label it as such.
5. **High:** the Chelan OCR pipeline is reusable but Chelan-hardcoded (discovery reads `DocType` codes, review/approval/
   cancellation pinned to Chelan: `nts_crawler.py:1467,1570,1603,1674`; matcher `nts_matcher_task.py:57`). A real
   parameterisation, not a vocabulary tweak. Verify free PDF access first.
6. **Medium:** "no grid answer" zeros are unverified; verdicts for divorce/code_violation say "no verified feed found",
   not "impossible". Divorce via Odyssey index is unassessed (terms unread). TAX DEED is post-foreclosure, not delinquency.

## Plan (each phase ≤5 files; owner approval between phases; Codex consult before, review after each)

- [x] **Phase 2: existing-scraper fixes** (owner-approved 2026-11-15; uncommitted). Files: `acclaimweb.py`, `registry.py`,
  `preforeclosure.py`, `taxsifter.py` + 3 test files.
  - [x] G1 `DouglasAcclaimWebScraper` (exact-host routing) reads grid `ParcelNumber` when exactly 11 digits; any other value
    is kept as `grid_parcel_unmatched`, never keyed. Other AcclaimWeb sites unchanged.
  - [x] Codex #2: TaxSifter owner-name fill refuses a lead whose own parcel differs from the hit
    (`taxsifter_owner_match=other_parcel`); the hit's parcel counts only when the assessor page echoes it.
  - [x] G2 `RELEASE LIS PENDENS` added to the cancellation guard (all counties).
  - [x] G3 Douglas probate adds `HEIRSHIP`, `TESTAMENTARY`; pre_foreclosure stays the shared map (doc_types partition intact).
  - [x] TOD: measured, no live exposure, rule unchanged (see baseline).
  - Verification: affected files 123 + 219 passed; ruff clean; full suite: see Review. Codex: NO-GO (retry identity across
    deploy, parcel echo, tests) -> fixed/answered -> **GO**.
  - **Deploy precondition (Codex):** zero PENDING/QUEUED/running Douglas jobs, and Douglas dispatch held (connector
    inactive or schedules paused) until every old worker has stopped; then re-enable.
- [x] **Phase 3: death_certificate** (commit 3bb24e41; Codex NO-GO -> fixed -> GO). Migration 170 (was 164; renumbered
  after merging main in 59f2a53d, ids fixed in 356e6e5d) appends the type to the
  (active) Douglas connector, so DEPLOYING = OFFERING it. Douglas DC rows: decedent is the person party on 20/21; one
  is "STATE OF WASHINGTON" -> "<NAME> ESTATE OF" (Douglas-only `strip_inverted_estate_caption`; the shared helper is
  unchanged because changing it would re-key other counties' delivered name+date leads). 6/21 carry an 11-digit parcel.
  Found and fixed while verifying live:
  - [x] unpaged grid lists one recording twice (identical but GUID; 10/19/2026) -> every run over that day failed. Collapsed.
  - [x] one-day window ran ZERO searches when `#RecordDate` was not yet on the page -> false zero "done". Always searched once.
  - [x] kendo single-picker fill stepped 7 days per one-date search (Codex). Steps a day.
  - Live: 10/19-11/04 read all 17 days, 0 DCs = matches the recorder's own doc-type search (0). Positive live read
    05/25-06/05 (2026-11-17): 3 records = the 3 the recorder lists, all naming a person (no agency, no "ESTATE OF"),
    no in-scrape address (held at delivery until the post-save lookups find one).
  - Open (pre-existing, not fixed): a first page that never shows a date field raises a PERMANENT
    ScraperExecutionError ("could not locate date inputs") on a slow portal; arguably transient.
- [x] **Phase 4: trustee_sale** (commits 356e6e5d, 22e74053; connector INACTIVE, not in the matcher). No OCR needed:
  Douglas SELLS its recording images (PDF endpoint "Error 5001", AddToCart block), but each recording's FREE detail page
  (in-session only; bare HTTP 401) carries the sale in the recorder's typed Remarks, plus grantor, related deed of trust
  and the 11-digit APN. `nts_douglas_recorder` (parser, lawful-sale-day rule, explicit sale construction, held reasons,
  discontinuance resolution), `DouglasAcclaimWebScraper.collect_by_doc_type` (one doc-type search per code; the portal's
  own form post must carry the requested type and dates; every row the requested type; cap on the reported total),
  `crawl_nts_douglas_recorder` (all-or-nothing, Pacific dates, 365-day window, heartbeat only after a complete write),
  beat 09:30 UTC, migration 172. NOCONT is the RCW 84.34 current-use continuance (no remarks, 2-10 APNs): ignored.
  - Live end-to-end crawl into the test DB (2026-11-17): 33 NTS + 16 NDTS read; 27 notices stored (12 active, 11
    inactive, 4 cancelled), 6 held (2 no_date, 3 no_sale_language, 1 several_parcels), 2 inferred ends, 3 unmatched
    discontinuances (3279193, 3280146, 3282060: their notices predate the window). A first run hit a portal timeout and
    wrote nothing (alerted) as designed.
  - Discontinuances: 5 of 16 link only the deed of trust / a modification. They end notices recorded BEFORE them on the
    same deed of trust, or the same parcel unless the deeds differ, reversibly. 3279193 (11/2025) shares its deed of
    trust with the LIVE notice 3286111 (11/2026, sale 3/19/2027), which stays live.
  - Codex: design consult; NO-GO x3 -> all fixed -> **GO for the inactive rollout**.
  - **Activation (owner):** after deploy let the crawl run; check the held/unmatched alerts and the stored notices; then
    set the trustee_sale connector active AND add "douglas" to `NTS_MATCH_COUNTIES` (code change + deploy). Codex's
    remaining activation item (two loans on one parcel) is fixed and tested; re-confirm on prod data.
  - Recorder banner (live): "Released through date: 09/16/2026"; later recordings are indexed but not yet "released".
- [ ] **Phase 5: tax_delinquent — needs owner decision (freshness).** Verified 2026-11-15:
  - Treasurer "2026 Notice and Summons" (Superior Court 26-2-00337-09, filed 9/17/2026; scanned, 11 pages). Exhibit A
    rows carry: 11-digit parcel, legal, street address, vested owner + mailing, taxpayer + mailing, certificate tax years,
    "Taxes and Interest Due As Of August 1, 2026". ~3 parcels per page, pages 6-11. Needs OCR (tesseract is in the prod
    image, not on this laptop).
  - TaxSifter Treasurer.aspx per parcel: per-statement balances due, receipts, page echoes the parcel. BUT the site footer
    says "Data current as of: 6/3/2026" (5 months stale), so it cannot prove a listed parcel is still unpaid TODAY.
    (Also: Douglas TaxSifter owner/mailing used by enrichment is June data.)
  - No current-status source found yet; Bid4Assets (the 2026 auction) would show remaining parcels but its terms are unread.
  Options: (1) deliver foreclosure-stage leads labelled "as of 8/1/2026 per the court summons", accepting that some may
  have redeemed; (2) find/approve a current source (Treasurer by phone/PRR, Bid4Assets terms); (3) hold.
  **Owner chose (1) 2026-11-15. Build attempted, then STOPPED as unsafe:**
  - The county keeps the summons PDF current: redeemed rows get a grey "REDEEMED" stamp and a diagonal strike line
    (page 8: 3 of 4 rows redeemed; PDF modified 2026-10-27). So the document DOES carry redemption status.
  - Tesseract (repo OCR child, run in a python:3.12-slim + tesseract 5.5 container) does not read the stamp, and the
    strike line destroys exactly the redeemed rows' parcel numbers. 16 parcel rows read; parser's one-years/one-amount
    guard fails closed on page 8 (a lost parcel pushes its values into the row above).
  - Unsafe case: a redeemed row whose parcel number survives OCR would ship as delinquent. Not wired, not enabled.
  - Saved, unmerged: branch `wip/douglas-tax-ocr-unsafe` (a stash commit; restore with
    `git stash apply wip/douglas-tax-ocr-unsafe`): OCR child accepts no-op CropBox/Rotate and 8-bit grey/RGB JPEG
    (header-checked, never decoded in the child); `taxsifter.lookup_parcel_owner` (parcel search + echo, owner/situs/
    mailing/"data current as of"); `douglas_wa_tax_delinquent.py` (summons discovery, Exhibit A parser, owner surname
    check, as-of labelling). No tests yet; no Codex review yet.
  - Safe ways forward: (a) per-version operator review of the summons rows (~16/yr; the county re-stamps as parcels
    redeem), Chelan-style; (b) export word boxes from the OCR child and detect the stamp/strike per row band (image
    analysis; harder to prove); (c) hold.
  Original options: between (a) OCR of the annual Notice and Summons (foreclosure-stage only),
  (b) a Public Records Request for the Treasurer's year-round delinquent file, (c) per-parcel TaxSifter balances.
  - 2026-11-17: owner asked to proceed; NOT built pending an explicit pick, because operator review means ongoing owner
    time (the county re-stamps the PDF as parcels redeem; OCR cannot segment struck rows, so an operator would also
    type them) for ~16 leads a year. A Ferry-style per-parcel TaxSifter tax crawl exists (`taxsifter.resolve_tax`) but
    has a 14-day source-date gate that Douglas's June-dated TaxSifter fails.
- Blocked (no code): **code_violation** (county SmartGov is complaint intake only; recorder VIOLATION = 0 in 12 months;
  cities' sites have no case data). **divorce** (recorder DIVORCE = 0 in 12 months; the court index is case-level only
  and GR 31(g)(4) bars commercial solicitation from bulk court records — the same blocker Ferry recorded; counsel
  needed). **death_certificate** certificates themselves are restricted (RCW 70.58A); recorder DC is the feed.

## Seven-category matrix (2026-11-17)

| Category | Source | Access | 12-mo volume | Status |
|---|---|---|---|---|
| probate | Recorder LACKPRO/DC/AFFIDE/WILL/LT/HEIRSHIP (+TOD opt-in) | public index | ~74 + 70 TOD | Existing, repaired (Phase 2); implemented, awaiting deploy |
| pre_foreclosure | Recorder NTS / LP (RELEASE LP excluded) | public index | 36 | Existing, repaired (parcel restored, TaxSifter gate); awaiting deploy |
| trustee_sale | Recorder NTS detail pages (typed Remarks) | free detail page, in-session | 33 | Implemented, verified live end to end; connector INACTIVE (activation steps above) |
| tax_delinquent | Treasurer 2026 summons (scanned, REDEEMED stamps) | public PDF | ~16 | Not implemented: owner decision (operator review vs hold) |
| code_violation | none found | - | 0 | Blocked by source access |
| divorce | court index (GR 31(g)(4)) | case index only | 0 recorded | Blocked by legal/data restrictions |
| death_certificate | Recorder DEATH CERTIFICATE | public index | 21 | Implemented, verified live (3/3); offered on deploy (migration 171) |

## Review (2026-11-16, interim)
- Full suite on 3bb24e41, fresh test DB (bl-douglas-pg recreated, alembic 164): 8,970 passed, 53 skipped, 1 failed +
  2 errors; all three (test_db_canary slow-answer timing, test_address_lookup_progress GIS sweep) pass in isolation
  (21/21). `tests/test_migrate_lock.py` deselected from full runs (it hangs inside a full run) and passes alone (6 + 1 skip).
- A run on a DB left dirty by two stopped runs gave 317 FK-violation failures: environment, not code. Recreate the DB
  after any aborted full run.
- Released: nothing (no push, no deploy). Phase 2 + 3 committed locally; tax WIP on `wip/douglas-tax-ocr-unsafe`.

## Review (2026-11-17)
- Merged origin/main (Cowlitz #652, Ferry #655, OCR JPEG #658, Asotin LP #661): migrations now 170 (death_certificate)
  and 171 (trustee_sale connector, inactive); single alembic head 171.
- Tests: 349 (merge) + 377 + 324 related passed; Douglas trustee tests 60/60; ruff clean.
- Playwright QA (local: branch API on the isolated test DB, bridgeleads-web origin/master on localhost:3000, a seeded
  business user and a prod-shaped Douglas connector): Douglas shows "3 types" = Probate, Pre-Foreclosure, Death
  Certificate; auction leads (inactive), tax, code violation, divorce absent; no horizontal overflow at 320/375/390/430/
  768/1024/1440/1920 px; no page errors. Dark mode NOT exercised: the app ignores the OS colour scheme (own toggle).
  Not exercised locally: job runs, results, exports (need a worker and live scrapes).
- Next.js dev server printed a known-vulnerability upgrade notice for bridgeleads-web's Next 16.4.3 (not acted on).
- Deploy order: migrations 171 + 172 (renumbered after #667 took 170); Phase 2 precondition (no queued/running Douglas job; hold Douglas dispatch until
  old workers stop); trustee_sale stays inactive until the activation steps above.
