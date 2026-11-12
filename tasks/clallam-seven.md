# Clallam County, WA — seven record types (branch `feat/clallam-seven-record-types`)

Worktree `C:/Users/micha/bl-wt/clallam-7`, based on origin/main `4e4b653c`. Research 2026-11-12:
read-only production SQL, public county pages, and one-off searches on the Clallam recorder
(no logins, no captchas, nothing purchased).

## Baseline (production, read-only, 2026-11-12)

Connector `3c1dc2fe` (EagleWeb template, `erecording.clallamcountywa.gov`) — active, healthy,
`record_types = [probate, pre_foreclosure]`. Customer runs ever:

| type | runs | status | records_found | saved | new | owner / parcel / property / mailing |
|---|---|---|---|---|---|---|
| pre_foreclosure | 1 (07/05-10/03/2026) | done | 1 | 1 | 1 | 1/1 complete (PACS mailing) |
| probate | 1 (10/01-10/31/2026, living-owner TOD on) | done | 3 | 3 | 3 | 3/3 complete; all 3 are Transfer-on-Death deeds |

0 failed runs, 0 NULL `records_found`, 0 duplicates. All parcels are 12-digit strings (statewide id);
PACS maps id+`0000` (PR #580). Volume is too small to prove much; the defects below come from the source.

## Source facts (recorder, last 12 months unless noted)

`DEATH` 85 · `TOD` 276 · `NTS` 73 · `NTSCL` 2 (5 yrs) · `LISP` 14 · `RLP` 5 · `OS` (Order of Sale) 4 ·
`JUDGE` 16 · `LETTR` ("Letter") 3 · `DISOL` 0 (8 in 5 yrs) · `DISS`, `EXEC` ("Writ of Exec"),
`NCR`, `DISN` 0 in 5 yrs. The search caps at 10,000 rows and a 5-year date range.

## Defects found in the existing integrations

1. **pre_foreclosure: discontinuances counted as active foreclosures.** Clallam records a
   Discontinuance of Trustee's Sale under the `NTS` type, with the grantee cell reading
   `<BORROWER> DISCONTINUANCE <original NTS no.>` (verified: 2026-1486992 discontinues
   2026-1484470). `is_cancellation_or_admin()` only sees the doc-type label, so the
   discontinuance becomes a lead with a polluted name, and the cancelled original stays a lead.
2. **probate: wrong Clallam codes in the keyword map.** `EXEC` is "Writ of Exec" (judgment
   collection), `SUCC` is "Sat of UCC-2", `LETTR` is a generic "Letter". `EXEC` would match
   writs as probate; all three currently match 0 rows in Clallam.
3. (all EagleWeb counties, not Clallam-only) `doc_type` stores `"<label>\n<instrument no.>"`.
   Noted, out of scope.
4. Result-completeness (silent partial page, pagination cap, verified zero) is being fixed in
   shared `eagleweb.py` by the Benton session (`feat/benton-wa-seven-record-types`, uncommitted).
   Not duplicated here; Clallam inherits it.

## Seven-type matrix (proposed)

| type | source | decision |
|---|---|---|
| probate | Recorder `DEATH` (+ `TOD` behind the existing living-owner toggle) | keep; fix defect 2 |
| pre_foreclosure | Recorder `NTS`, `NTSCL`, `LISP` | keep; fix defect 1 |
| death_certificate | Recorder `DEATH` (same as King's probate-family death_certificate) | build: EagleWeb keyword key; connector flip needs owner approval |
| tax_delinquent | Treasurer Certificate of Delinquency PDF (tax-foreclosure stage), resolved from `/1077/Tax-Foreclosure-Sales` | build new module; 8 parcels as of 06-Nov-26; connector needs owner approval |
| trustee_sale | Sale date and amount exist only inside the recorded NTS image; legal newspaper on wapublicnotices.com bars scraping | blocked by source access |
| divorce | Recorder ~0/yr; court Odyssey portal is attorney-only subscription; JIS-Link bars building databases | blocked by legal/source |
| code_violation | No public case list for the county or for Port Angeles, Sequim, Forks; records request only | blocked by source access |

## Codex consult (2026-11-12) — reconciled
- Discontinuance must be read from the raw grantee/summary BEFORE party orientation, keyed by the
  full instrument (`2026-1484470`, year kept), and must also catch discontinuances recorded after
  the window. Adopted: per surviving NTS candidate, search the recorder by its instrument number
  (live-verified: that search returns the original AND its discontinuance) and drop it if cancelled.
  Already-saved leads are not re-checked (stated limit).
- Do not delete `EXEC/SUCC/LETTR` from the shared map (other counties may use them). Adopted:
  exclude "WRIT OF EXEC" from probate (never probate anywhere); `SUCC`/`LETTR` match no Clallam label.
- death_certificate needs: no parcel requirement like probate, probate orientation, TOD excluded,
  connector `record_types` membership; test dedup across probate + death_certificate.
- tax_delinquent: the tax pipeline requires `bill_year` + `delinquent_amount` + a qualified source
  (`tasks_helpers/dedup.py:224-262`); the certificate PDF has no bill year, and it is a dated snapshot
  that the Nov 17-19 sale obsoletes. Needs an owner decision before building.
- trustee_sale / divorce / code_violation stay disabled (agreed).

## Phase 1 (≤5 files) — fix the two live integrations
- [x] Codex consult on this plan
- [x] Based on 60c75d4d (Benton session's EagleWeb pagination fix: the old loop stopped at
      the first page with no matching type; Clallam shows 5 rows a page, so runs read ~1 page)
- [x] pre_foreclosure: skip discontinuance rows; on Clallam look up each deliverable NTS by
      instrument (whole result set, rows must equal the portal total) and drop cancelled ones
- [x] probate: exclude "WRIT OF EXEC" and "REVOCATION OF TRANSFER ON DEATH"
- [x] tests: 7 new (real captured pages, routed Chromium); 56 pass with pagination/probate suites; ruff clean.
      DB-free only: no local test DB exists (standing constraint). No routed scrape() test (Codex Low):
      detail fetch uses real HTTP and the testing rules forbid mocking it; wiring proven live instead.
- [x] live (no DB writes), 09/01-11/12/2026: pre_foreclosure 0 -> 13 (14 NTS = 10 kept + 4
      discontinuances; + 3 lis pendens); probate 0 -> 76 (59 TOD, 17 Death; 3 revocations removed).
      07/01-07/14/2026: NTS 2026-1484470 dropped via lookup (discontinued 09/18, after the window).
- [x] Codex review: NO-GO (1 High, 1 Medium, 1 Low) on 00b9d93b -> fixed in b2ab050b -> GO

## Phase 2 — new types
- [x] death_certificate via EagleWeb (4c280eb7, 405024ea, 3c3af382, 8aa24213). Whole-label gate:
      Death / Death Certificate / Certificate of Death only. Live 09/01-11/12/2026: 17 = the 17 Death
      rows (10 with a parcel, 7 name-only "PUBLIC SEE RECORD"). Codex: NO-GO (Affidavit of Death and
      hyphenated TOD passed) -> fixed; my instrument-year fix changed the in-job retry hash -> reverted,
      left as a shortcut note.
- [x] tax_delinquent: `src/scrapers/clallam_wa_tax_delinquent.py` (e468a272, 13f23330, 767be95e).
      Newest certificate from the Treasurer page -> PDF per page -> each PID re-read on the property
      portal (Geographic ID must match; "No Amount Due" = redeemed; prior-year balances -> bill_year
      + amount; status date must be current) -> 18-month cap unless exempt. Live 2026-11-12: 8
      parcels -> 0 leads (capped); exempt -> 8 complete leads. Codex: NO-GO twice -> fixed -> GO.
- [x] Connector rows NOT written: activation is an owner decision (see Review).

## Phase 3 — report
- [ ] Playwright UI checks: not run. No frontend change, and nothing is activated, so the live UI
      cannot show the new types yet; run after activation (needs a test account, no paid lookups).
- [x] cross-cutting Codex challenge: NO-GO (2 High, 1 Medium) -> fixed in 767be95e -> GO for merge.

## Final seven-type matrix (2026-11-12)

| type | agency / source | access | adapter | owner/parcel/address | tests | Codex | status |
|---|---|---|---|---|---|---|---|
| probate | Auditor EagleWeb (erecording.clallamcountywa.gov) `DEATH`, `TOD` | public, no captcha, no anti-automation terms | existing template, fixed | recorder parties; parcel via detail page; PACS mailing | 8 + pagination suite | GO | **Implemented, awaiting validation** (merge + prod run) |
| pre_foreclosure | same, `NTS`/`NTSCL`/`LISP` | same | existing template, fixed | 13/13 live with parcel | 7 + pagination suite | GO | **Implemented, awaiting validation** |
| death_certificate | same, `DEATH` | same | new type in template | 10/17 with parcel; name-only rows rely on enrichment | 9 | GO | **Implemented, awaiting validation** (connector off) |
| tax_delinquent | Treasurer certificate + Assessor/Treasurer PACS portal | public | new module | 8/8 owner, situs, mailing; 12-digit id for 0000 rows | 19 | GO | **Partial**: blocked by the 18-month cap (owner decision) |
| trustee_sale | sale date/amount only in recorded NTS images; legal notices on wapublicnotices.com (bars scraping, $10k/incident) | blocked | none | - | - | agreed | **Blocked by source access** |
| divorce | Superior Court: Odyssey portal attorney-only subscription; recorder 0/yr (8 in 5 yrs) | blocked | none | - | - | agreed | **Blocked by legal/data restrictions** |
| code_violation | county DCD, Port Angeles PD: complaint intake only, no public case list | blocked | none | - | - | agreed | **Blocked by source access** |

## Review

**Root causes found.** (1) The shared EagleWeb loop stopped at the first results page with no
matching type; Clallam shows 5 rows a page, so Clallam runs read about one page (live: 0 records
where the recorder had 14 NTS). Fixed by the Benton session (60c75d4d), which this branch is based on.
(2) Clallam files Discontinuances of Trustee's Sale under its NTS type; they became pre-foreclosure
leads and the cancelled notices stayed live. (3) Probate matched "Writ of Exec" and TOD revocations.

**Baseline.** Production, ever: 2 Clallam customer runs, 4 leads (1 NTS, 3 TOD), all complete,
0 failed, 0 NULL records_found. Live after: pre_foreclosure 0 -> 13, probate 0 -> 76 for
09/01-11/12/2026; death_certificate 17; tax 8 (0 under the cap).

**Owner decisions / prerequisites.**
1. Merge order: 60c75d4d (Benton, local/unpushed) must land first or with this branch.
2. Activate death_certificate: add it to Clallam connector 3c1dc2fe `record_types` (changes public coverage).
3. Tax: exempt `clallam_county_tax_foreclosure_certificate` from the 18-month cap (TAX_CAP_EXEMPT_SOURCES)
   or keep tax unavailable; then a manual-mode connector row for ClallamWATaxDelinquentScraper.
   The certificate refreshes once a year (filed ~September; sale in November).
4. trustee_sale / divorce / code_violation: only through licensing or county agreements
   (newspaper legal-notice license, AOC data-dissemination contract, records-request feed).

**Known limits.** Leads delivered before this fix are not re-checked for discontinuances. Name-only
death certificates (no parcel) depend on enrichment to qualify. Billing dedup keys on parcel|address
(frozen), so a tax and a recorder lead for one property may not collapse when addresses differ.
DB-backed tests (progress reporting, billing, dedup) could not run locally: no test database.
