# Ferry County, WA: seven record types (branch `feat/ferry-wa-seven-record-types`)

Worktree `C:\Users\micha\bl-wt\ferry-7`, from origin/main c8fcfecb. Research 2026-11-15: plain HTTP reads of public
pages plus read-only Playwright (Chromium) searches on the State Digital Archives. No logins, no purchases, no
disclaimers clicked by automation (an automated TaxSifter "I Agree" was refused by the session permission
classifier; not worked around).

## Starting state
- No Ferry scraper, connector migration or plan in the repo. Ferry appears only as FIPS 019 (`county_gis.py`) and in
  an April 2026 audit note ("non-standard portal path"). A prod `county_connectors` row may still exist from that
  era (not checked; read-only check needs owner OK).
- Reusable: `src/scrapers/enrichment/taxsifter.py` (TerraScan TaxSifter on PublicAccessNow: Douglas, Okanogan
  owner-name fill; Whitman parcel-keyed mailing). Ferry runs the same PublicAccessNow product
  (`ferrywa-taxsifter.publicaccessnow.com`, disclaimer version 1.0.9607).

## Sources verified

| Source | URL | Access | Terms / restriction found | Verdict |
|---|---|---|---|---|
| Auditor recording | ferry-county.com/departments/auditor/recording.php | **No online index.** Copies by emailed/paper request, $8/hr search, $1/page | Request form: requester declares "use of public documents containing lists of individuals for a commercial purpose violates Washington State Law" and agrees not to use the information for profit-making contact (RCW 42.56.070(8)) | Recorder route **blocked**: no index to read, and the only route carries a signed commercial-use pledge |
| Assessor/Treasurer TaxSifter | ferrywa-taxsifter.publicaccessnow.com | Public, disclaimer click-through | Disclaimer: "as is", accuracy, indemnity. **No commercial-use or automation clause** (read in full) | Disclaimer silence is not permission: reuse rights UNRESOLVED (same class as Douglas/Okanogan, register row 42, owner clearance 2026-10-02 with no counsel memo); record a Ferry-specific basis before activation |
| Treasurer tax foreclosure | ferry-county.com/departments/treasurer/index.php | Page announces 2026 sale **Nov 24 2026, 10 a.m.**; says properties are "listed below" but **no 2026 list is published** (document center holds only 2021 and 2022 lists) | none on page | No list to read; per-parcel TaxSifter only |
| Superior Court index (State Archives) | digitalarchives.wa.gov/Collections/TitleInfo/3007 | Public, free, no login. Year search returns docket rows: case no., date, docket code, description, case title, county | Policies page: no commercial or automation clause. robots.txt disallows `/Record/` (detail pages) only; `/Search` allowed. Sealed/adoption/dependency/mental-competency excluded by the Archives | Usable index, but **stale** (below) |
| WA courts Odyssey | odysseyportal.courts.wa.gov | Registration; per-name lookup | GR 31; repo policy: not scraped (`docs/compliance/wa-court-records.md`) | Not used |
| Legal newspaper | Ferry County View (ferrycountyview.com) | No legal-notice section; e-edition is a paid subscription (newstogo) | n/a | No free source |
| WNPA aggregator | wapublicnotices.com (Ferry County View paperid 1119 carries NTS) | Public | **Terms ban scraping** (liquidated damages); repo already rejected it (BUILD_JOURNAL ~L393, `nts_crawler.py:74`) | **Blocked** |
| Planning & Building | ferry-county.com/departments/planning___building | Ordinances, applications, GovQA records requests | No case-level enforcement list | **Blocked** |
| Vital records | Auditor page: death certificates handled by NE Tri-County Health District; WA DOH restricted (RCW 70.58A) | Qualified applicants, paid | n/a | **Blocked** |

### Digital Archives Ferry index, measured (search year 2026, all Ferry rows, 820 unique docket rows)
- Newest docket date **2026-06-12**; zero cases numbered `26-*`. Case types seen: 1 criminal, 2 civil, 3 domestic,
  4 probate/guardianship. The index trails the clerk by about **5 months**, and 2026 filings are not in it yet.
- Probate (`-4-`): 14 cases with 2026 activity, 9 titled "ESTATE OF", the rest guardianships (must exclude).
  Docket codes give classification: NTAPTP (notice of appointment and pendency of probate), CRDCLM (creditor
  claims), DCLCMP (declaration of completion, i.e. closed).
- Domestic (`-3-`): 20 cases, mostly parenting-plan / support modifications; 3 "DECREE OF DISSOLUTION" entries.
  Dissolution vs parentage vs modification must be told apart by title + docket code.
- Result paging: `ResultsTable` `page` is 1-based and clamps (page 0 = page 1; past the end repeats the last page);
  a scraper must reconcile against the reported count, never trust page length.

## Seven-type feasibility

| Type | Source | Status | Why |
|---|---|---|---|
| probate | Digital Archives court index (`-4-`, "ESTATE OF", NTAPTP) + TaxSifter owner match | **Buildable, stale** | ~5-month lag; no 2026 filings yet. Estate cases stay open months, so some leads remain actionable. Owner-name match needs TaxSifter. |
| divorce | Digital Archives (`-3-`, dissolution only) | **Buildable, stale + policy** | Same lag. Repo policy (GR 31 note) never built divorce from court indexes; owner call. Party-to-parcel match is name-only. |
| pre_foreclosure | Recorder index | **Blocked by source access** | No online recorder index; request route carries the RCW 42.56.070(8) pledge. Lis pendens would appear as civil (`-2-`) cases in the Archives, 5 months late. |
| trustee_sale | Recorder / newspaper | **Blocked by source access + terms** | No recorder index; newspaper online only via wapublicnotices (scraping banned) or paid e-edition. |
| tax_delinquent | TaxSifter per-parcel tax pages | **Blocked pending owner OK** | No published 2026 list. Per-parcel crawl (Asotin pattern) needs the TaxSifter disclaimer accepted by automation, which this session was refused, and a broad-crawl approval. Field availability (delinquent years, foreclosure flag) not yet seen. |
| code_violation | none | **Blocked by source access** | No case-level public data (county or Republic). County authority over parcels inside the Colville Reservation depends on land tenure (trust, restricted, fee); not settled by geography. |
| death_certificate | none | **Blocked by legal/source access** | DOH restricted; no recorder index. |

## Proposed phases (each <= 5 files, stop for approval between phases)
- **Phase 1: shared Ferry property resolver.** Add `ferry` to `TAXSIFTER_ORIGINS` (owner-name fill) and/or
  `TAXSIFTER_PARCEL_SITES` after a live markup check. Needs owner OK for the disclaimer click-through.
- **Phase 2: Digital Archives court-index source** (`src/scrapers/sources/wa_digital_archives.py`): POST search,
  title filter, 1-based paging reconciled to the reported count, docket-row parser, case-type and docket-code
  classifier. Pure-parser tests on saved real pages (names redacted). Reusable by 17 counties.
- **Phase 3: Ferry probate scraper** (estates only, guardianships excluded, closed estates (DCLCMP) dropped),
  inactive connector migration.
- **Phase 4 (only if owner approves): divorce** (dissolution only).
- **Phase 5 (only if owner approves): tax_delinquent** per-parcel TaxSifter crawl.
- Blocked types: no adapters, no connector rows; documented here.

## Owner decisions needed
1. Is a court index that trails filings by about 5 months acceptable for Ferry probate (and divorce)? If yes,
   should leads be labeled with the docket date, not "new filing"?
2. Divorce from a court index: the repo has never done this (GR 31 note). Build or keep blocked?
3. TaxSifter: OK to accept its disclaimer by automation (as Douglas/Okanogan/Whitman code already does)?
4. tax_delinquent: approve a rate-limited per-parcel crawl of Ferry TaxSifter (first broad run separately)?
5. Read-only check of prod `county_connectors` for an old Ferry row?

## Codex consult (Review A, 2026-11-15): "revise before implementation"
Verified by Claude against primary text where marked.
- **Critical (verified):** GR 31(c)(4) makes any "index, calendar, docket, register of actions" a court record;
  GR 31(c)(3) defines bulk distribution as "all, or a significant subset" of court records; GR 31(g)(4):
  "The use of court records, distributed in bulk form, for the purpose of commercial solicitation of individuals
  named in the court records is prohibited." A crawler that enumerates Ferry's docket index to market to estates
  or divorcing parties fits this closely. The Archives' silence on reuse does not override a court rule.
  **probate and divorce from the Archives index -> Blocked by legal/commercial-use conditions, pending counsel.**
- High: request-form pledge vs online indexes. Agreed: the Auditor pledge binds that request route only; absence of a
  clause elsewhere is not permission. Per-source authority is recorded in the source table above (RCW 42.56.070(8)
  for agency list disclosure; RCW 65.04 for recorder indexes; GR 31 for court records).
- High: TaxSifter owner-name lookup returns no parcel id (`taxsifter.py:278`), so a name match cannot become the
  billing identity; enrichment failure currently reads as "no match". Applies only if a name-matched type is built.
- High: case-level aggregation, closure status "unknown" when the index is stale, explicit coverage metadata to stop
  false zeros, docket-event vs filing date vs ingestion date. Applies only if the Archives route is ever cleared.
- Medium: "blocked" should read "no lawful free source established". Not yet checked: direct Ferry County View
  licensing, Sheriff/court sale notices, trustee-published notices, a Treasurer delinquency export by request.
- Medium: check prod connector rows before any activation (oldest active matching row wins, `registry.py:69`).

## Revised feasibility (after Review A)
| Type | Status |
|---|---|
| probate | Blocked by legal/commercial-use conditions (GR 31(g)(4)); also ~5-month index lag |
| divorce | Blocked by legal/commercial-use conditions (GR 31(g)(4)); also ~5-month index lag |
| pre_foreclosure | Blocked by source access (no recorder index; request route carries the commercial-use pledge) |
| trustee_sale | Blocked by source access + terms (no recorder index; WNPA bans scraping; newspaper e-edition paid, licensing unasked) |
| tax_delinquent | Not implemented: needs owner OK for automated TaxSifter disclaimer + per-parcel crawl; tax fields unverified |
| code_violation | Blocked by source access (no case-level data, county or Republic) |
| death_certificate | Blocked by legal/source access (DOH restricted; no recorder index) |

## Owner decisions (2026-11-15)
1. TaxSifter automated disclaimer + per-parcel tax crawl: **yes** (first broad run still separate).
2. Court-index probate/divorce: **build** (connectors off; GR 31 still pending counsel).
3. Outreach to the Treasurer and the Ferry County View: **both** (drafts below; Gmail connector needed
   re-auth, so they were not created as drafts and nothing was sent).

## Build (branch commits, local only, not pushed)
- `b5659547` tax_delinquent: `taxsifter.resolve_tax` / `parse_parcel_treasurer` (Treasurer "5 Year Tax History",
  real-property rows only, any unknown row shape -> unparsed), shared `_parcel_pass` with Whitman's mailing pass,
  crawler dispatch `_TAX_SOURCE`, `FerryWATaxDelinquentScraper` (Asotin contract), trusted source
  `ferry_taxsifter_tax`. Codex: NO-GO (ViewState in fixtures decoded to owner/address; malformed rows -> false
  zero; personal property) -> fixed by amend -> GO.
- `5dbf636d` merge origin/main (migration head 162).
- `ef85ea27` probate/divorce: `sources/wa_digital_archives` + `ferry_wa_court_index` + migration 163 (three
  inactive connectors, fixed ids). Codex: NO-GO (case-history false zero; motion-to-close read as closed; untested
  pager-less branch; downgrade scope) -> fixed -> GO.

## Measured (live, read-only, 2026-11-15)
- TaxSifter: 12/12 random parcels `found`; 1 owes a prior year; **7/12 have no situs address** (rural land). The
  hold rule (owner + situs + mailing required) holds those, so most Ferry delinquent parcels would be held.
- Ferry inventory: 9,195 parcels (statewide layer FIPS 019, 9,229 features). 400 parcels/hour -> ~23 h per sweep.
- Archives: year 2026 = 825/825 rows reconciled (3 legacy dash-less case numbers); newest docket 2026-06-12;
  newest case opening 2025-11 (no 2026 filings). 2025: 15 new probate/guardianship cases, 25 domestic.

## Readiness matrix
| Type | Agency / source | Access | Commercial-use status | Status | Remaining blocker |
|---|---|---|---|---|---|
| tax_delinquent | Ferry Treasurer, TaxSifter Treasurer pages | HTTP per parcel, disclaimer click (owner-approved) | UNRESOLVED: disclaimer has no commercial/automation clause, which is not a grant; RCW 42.56.070(8) concerns agency list disclosure; owner clearance 2026-10-02 (no counsel memo) | **Implemented, awaiting validation** (connector inactive) | First broad crawl (owner approval; Ferry is NOT in the crawler's schedule yet), >=95% coverage, rural parcels with no situs (job fails while any are held), Reservation trust/fee parcel rule, source-rights register entry |
| probate | WA State Archives Superior Court index (Ferry Clerk) | Playwright, public | **GR 31(g)(4)** bars commercial solicitation from bulk court records | **Blocked by legal/commercial-use conditions** (code built, connector inactive, every lead held) | Counsel on GR 31; a court-to-parcel matcher; index lag ~12 months for new filings |
| divorce | same | same | same | **Blocked by legal/commercial-use conditions** (same) | same |
| pre_foreclosure | Ferry Auditor recording | No online index; request form only | Request form pledges no commercial use of lists | **Blocked by source access** | A recorder index or a licensed feed |
| trustee_sale | Auditor / Ferry County View | No recorder index; newspaper e-edition paid; WNPA bans scraping | n/a | **Blocked by source access** | Newspaper licensing (draft below) |
| code_violation | Ferry Planning & Building; City of Republic | No case-level public data | n/a | **Blocked by source access** | a GovQA records request is the only untried route; Reservation parcels need a tenure-based rule before any type activates |
| death_certificate | WA DOH; NE Tri-County Health District | Restricted (RCW 70.58A), paid, qualified applicants | n/a | **Blocked by legal/source access** | none lawful |

## Codex evidence reviews of the blocked types (2026-11-15): all four AGREE-BLOCKED
Wording: "no usable lawful commercial feed established", not "no lawful source exists".
- pre_foreclosure: the request form's pledge cannot be signed for lead generation. Candidate, unverified for Ferry and
  for redistribution rights: trustee portals (Quality Loan sales portal, registration).
- trustee_sale: candidates LOGS/Aztec "Washington Foreclosure Sales Report" (Power BI; disclaimer bars unwanted
  owner contact), Quality Loan portal, Ferry County View licensing. Sheriff page has no sale calendar.
- code_violation: candidate a GovQA records request for enforcement logs (disclose the commercial purpose); NE
  Tri-County Health Accela holds septic permits, which are not violations. Reservation jurisdiction is not settled by
  geography alone.
- death_certificate: DOH sells noncertified informational copies to anyone ($25+, per known decedent; not a discovery
  feed); RCW 70.58A.520(6) data agreements keep the commercial-list restriction (subsection 19).

## Codex final adversarial review (Review C, 2026-11-15): NO-GO -> fixed
- High: Ferry in the crawler schedule would start the broad crawl on deploy -> Ferry removed from COUNTIES (wired, not scheduled).
- High: freshness counted fetch time only -> Ferry rows also need TaxSifter's own as-of date within 14 days and at most one day ahead (timezone slack).
- High: rural parcels without situs silently dropped -> Ferry tax job fails with the held count (FAIL_ON_HELD).
- High: court gate claimed verified zeros -> a court-index window with no filing now fails ("not a verified zero").
- Medium: inventory not reconciled -> statewide read must match the layer's own count (Asotin 13,612 and Ferry 9,195 verified live).
- Medium: taxpayer vs owner -> partly disputed: the code reads TaxSifter's "Owner Name" block, not the Taxpayer column;
  documented as not independently verified title.
- Medium: plan wording on rights and Reservation tenure -> rewritten above.

## Outreach drafts (not sent; Gmail connector needs re-auth)
To treasurer@co.ferry.wa.us, "Request: delinquent property tax list and data-use terms": asks (1) whether a current
delinquent-parcel list exists and in what format, (2) a data agreement/licensing route for commercial use,
(3) whether the Nov 24 2026 sale list will be posted, (4) whether a slow automated read of TaxSifter pages is
acceptable, citing RCW 42.56.070.
To editor@ferrycountyview.com, "Licensing question: legal notices (trustee sales, probate notices)": asks whether
the paper licenses a weekly digital copy or feed of trustee-sale and probate notices for commercial use, format, price.

## Review
- Tests: tax 25 (test_taxsifter_tax 18 + test_ferry_tax_delinquent 7... see gates), court 49 + migration 5;
  neighbouring suites green; ruff clean. Gates ledger in the session scratchpad.
- Not done: Playwright UI QA (all connectors inactive, nothing visible to customers); prod read-only connector check
  (denied by the session permission classifier); full test suite; push/PR (not authorized).
