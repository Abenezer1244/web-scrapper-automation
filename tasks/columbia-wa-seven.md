# Columbia County, WA — seven record types (branch `feat/columbia-wa-seven-record-types`)

Worktree `C:\Users\micha\bl-wt\columbia-7`, base origin/main d7188fe2. Discovery 2026-11-14:
plain HTTP + headless Chromium reads of public pages, no logins, no payments, no forms beyond
the public search forms.

## What already exists (prod, read-only check 2026-11-14)
- Connector `57cd7eed-f3db-4fca-8ee7-d4d64ce57eca`: iDocMarket COLWA1, `record_types = {probate, pre_foreclosure}`,
  template mode, **active**, `health_status = down`, `assessor_url` NULL. **Zero jobs ever.**
- Template `src/scrapers/templates/idocmarket.py` (Columbia is its only user).

## Findings (live)
1. **iDocMarket is broken for us**: the date fields are now `<duet-date-picker>` web components;
   `page.fill("#StartRecordDate")` throws. Typing into the inner `#start-record-date` /
   `#end-record-date` inputs fills the hidden `StartRecordDate`/`EndRecordDate` (ISO). Verified:
   08/16-11/13/2026 -> 253 docs; 01/01-11/13/2026 -> "Showing: 858 of 858 results", one page, no pager.
2. **Silent-failure gaps** in the template: a missing "Showing A of B" counter returns [] as a zero;
   shown < total only logs a warning (partial delivered as complete).
3. **Probate party is wrong on PR deeds**: grantor is the personal representative
   ("BREIER, MICHAEL, PERSONAL REPRESENTATIVE OF" on 4 deeds); `_clean_party` strips the role and
   the name lookup would search the PR's own property. Death certificates are inside the probate map.
4. **PACS (Harris PropertyAccess 9.0.50) works with the existing modules, plus one Columbia rule**:
   `Property.aspx` returns HTTP 500 without `&year=`; with `year=2026` ("2026 - 2027", the year the
   county says shows current ownership) `parse_pacs_detail_html` reads Geo ID, situs, mailing
   (incl. 50/50 owners) and `parse_tax` reads statements `found`, as of 11/14/2026.
   Search form default `taxyear=2026` (newest). Geo IDs are **13 digits** (not the 16 in the brief),
   equal to statewide `ORIG_PARCEL_ID` (3/3); statewide FIPS 013 lists 5,477 parcels.
   Disclaimer cites RCW 42.56 authorization only, no 42.56.070(8) commercial clause (as Asotin).
5. **Owner-name lookup is safe but weak here**: exact single-row owner match only. Decedents are
   usually off the title already; rural parcels have no situs. The recorded legal
   ("SYNDICATE 9 5", "T09N R40E 23 LOT 3") matched the right PACS parcel in the 2 cases checked.

## Feasibility matrix (live counts 08/16-11/13/2026)

| Type | Source | Index rows (90 d) | Status | Why |
|---|---|---|---|---|
| probate | iDocMarket recorder: PERSONAL REPRESENTATIVE DEED, LACK OF PROBATE AFFIDAVIT | 7 + 2 | Fix + keep | Decedent must be the party, never the PR |
| death_certificate | iDocMarket CERTIFICATE OF DEATH (public recording) | 9 | Build (template map split) | Whole-label gate, as EagleWeb/Chelan |
| pre_foreclosure | iDocMarket NOTICE OF TRUSTEE'S SALE (no NOD / LP seen in 2026) | 2 | Fix (date picker) | Borrower only in the image when beneficiary->trustee rows |
| trustee_sale | NTS sale date is only in the image (iDocMarket subscription) or the Dayton Chronicle legals (wapublicnotices.com, paperid 1066; not confirmed free, no NTS seen) | 2 | Blocked by source access | Never invent a sale date |
| tax_delinquent | PACS per-parcel crawl (Asotin crawler); Treasurer 2026 list = 2 parcels, sale 12/30/2026 (cross-check only) | n/a | Build | Prior-year arrears rule as Asotin |
| divorce | Superior Court: Odyssey Portal = paid annual subscription ($100+/yr, signed agreement); Digital Archives 2291 = case-number/keyword search, per-page fees, no date-range index confirmed | n/a | Blocked by source access | GR 31; no free filing-date index |
| code_violation | County ch. 19.05/19.30 ordinances, Dayton code-compliance page: complaint info only | n/a | Blocked by source access | No public case log |

Court probate (as opposed to recorder probate) is blocked the same way as divorce.
Death certificate: Public Health sells certified copies to eligible applicants only (2013+); no
public index. Our death_certificate = the publicly RECORDED certificate in the Auditor index.

## Plan (phases, <=5 files each, approval between phases)
- [x] **Phase 1 — recorder repair** (done 2026-11-14, pending Codex gate). Live run 08/16-11/13 with the
  patched scraper: probate 2 (Lack of Probate Affidavits), death_certificate 9, pre_foreclosure 1
  (the other NTS indexes lender->trustee, no borrower: dropped). Also found + fixed: record date moved
  to `<p aria-label="Record Date: ...">` (every row had date None); kept rows without date/doc# now
  fail as schema drift; ", DECEASED" stripped. Tests: 18 browser/unit tests, 10 of 16 originals fail
  on the old template (negative control).
  **iDocMarket answered 403 Forbidden after ~12 searches in an hour (2026-11-14).** The template
  fails closed on it. Read iDocMarket Site Terms on automated access before customer use (owner).
  Original Phase 1 scope: (`templates/idocmarket.py`, its test, `doc_types.py` if needed):
  date-picker fill + hidden-field verify; counter required (missing -> raise, shown<total -> raise);
  probate party = decedent ("ESTATE OF X", "X, DECEASED", affidavit grantor), PR goes to `heirs`,
  rows with only a PR name are not name-looked-up; split CERTIFICATE OF DEATH into `death_certificate`.
  Offline fixtures from the saved live HTML.
- [x] **Phase 2 — Columbia PACS** (2026-11-14, pending Codex gate). `pacs.ownership_year()` reads the
  newest `taxyear` option from the county's own form (host list `_YEAR_HOSTS`, Columbia only; other
  portals unchanged); both requests of both paths name it; no year offered -> name path FAILED, parcel
  pass deferred (never a miss). Live: mailing 3/3 found (incl. Richland out-of-county, 50/50 co-owner),
  bogus parcel `parcel_not_found`; tax 2/2 found as of 11/14/2026; exact unique name found, rural
  no-situs owner no_match. Tests: 200 pass on a disposable CI-equivalent DB; 5 of 7 new Columbia tests
  fail on the old code. Phase 3 note: tax situs can be locality-only ("DAYTON, WA 99328") -> must hold.
  Original Phase 2 scope: (`pacs_parcel.py` site + detail `year`, `pacs.py` detail URL year,
  `county_gis.py` mailing county list, tests): connector `assessor_url`.
- [ ] **Phase 3 — tax_delinquent** (`pacs_tax_crawler.COUNTIES`, `columbia_wa_tax_delinquent.py`,
  registry, migration 158 seeding an INACTIVE connector, tests).
- [ ] **Phase 4 — migration** adding `death_certificate` to connector 57cd7eed + `assessor_url`.
- [ ] Trustee_sale / divorce / code_violation: build only if research finds a free, current source.
- [ ] Codex review per phase; final adversarial review; journal entry.

## Codex consult r1 (2026-11-14, `bl-wt/codex_columbia_consult_r1.txt`), folded in
- High: year must reach BOTH detail paths (`pacs_parcel.py:360`, `pacs.py:262`); optional, absent for other counties.
- High: no hardcoded 2026. Read the county's newest taxyear option from the search form; fail visibly if absent.
- High: counter rule = `shown == total == cards`; only explicit `0 of 0` with no cards is a zero. Browser-run fixture test.
- High: PR-only rows need an enforced lookup restriction (`enrich.py:656`), not just a party swap; affidavit grantor is not proven decedent.
- High: PR deeds may be completed transfers — product decision (below).
- High: death_certificate split vs billing dedup `(user_id, dedup_hash)` — Columbia has 0 jobs, so no history collides; test anyway.
- High: register `columbia_pacs_tax` in `_TRUSTED_TAX_SOURCES` (`dedup.py:165`).
- Medium: `COUNTIES` tuple starts the hourly crawl even with an inactive connector — gate on approval.

## Owner decisions (2026-11-14)
- PR deeds: DROP from probate (completed sales; never look up the PR). Probate = Lack of Probate Affidavit.
- tax_delinquent: build AND enable (hourly crawl on deploy; connector live after coverage >= 95%).
- trustee_sale / divorce: blocked; owner reviewing subscription pricing.
- General delivery hold: separate PR later.

## Decisions needed from the owner (original questions)
1. Delivery hold for recorder leads without verified owner/parcel/address (no general hold exists; Asotin Codex finding).
2. Legal-description -> parcel resolver (stronger than name match; new code) — build now or later?
3. tax_delinquent per-parcel crawl of 5,477 parcels, paced like Asotin — approve the first broad run.
