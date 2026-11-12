# Chelan County, WA — seven record types (branch `feat/chelan-seven-record-types`, worktree `C:\Users\micha\bl-wt\chelan-7`)

Research and live reads done 2026-11-12: read-only, no logins, nothing bought, nothing written to production.

## Baseline (production, read-only SQL, 2026-11-12)

Connector `chelan` (AcclaimWeb template, `https://acclaim.co.chelan.wa.us/acclaimweb`): active, health `healthy`,
record_types `[probate, pre_foreclosure]`, `max_date_range_days` 30, `assessor_url` NULL.

| Job | Type | Window | Status | records_found | saved | no_address |
|---|---|---|---|---|---|---|
| 5a064b38 (2026-09-13) | probate | 08/14–09/13 | done | **NULL** | 0 | NULL |
| f171b25b (2026-09-13) | pre_foreclosure | 08/14–09/13 | cancelled | NULL | 0 | NULL |
| 005c4bd2 (2026-10-12) | probate | 09/12–10/12 | done | 4 | 0 | 4 |

So: 3 runs total, 2 marked done, 1 cancelled. 1 "done" run has a NULL `records_found` (a false success). 4 source records found,
0 qualified, and 0 ever delivered. Pre-foreclosure has never completed a run. There are no Chelan rows in `nts_notices` or `county_records`.

## Defects verified live (2026-11-12)

| # | Defect | Evidence | Severity |
|---|---|---|---|
| D1 | **Stale grid read**: each day's search reads the previous search's grid | Searched 10/07 and got the 10/06 rows; searched 10/08 and got the 10/07 rows (`chelan_pager.py`) | Critical: rows are attributed to the wrong day, and the last day of every window is never read |
| D2 | **Pagination never advances** on Chelan's Telerik grid (`t-arrow-next`); the selectors are Kendo-only | Each day shows "Displaying items 1 - 11 of 43" (4 pages), but pages visited = 1 | Critical: about 75% of rows are never read |
| D3 | `_extract_all_pages` stops when a page has 0 rows of the *requested type* | `new_count` counts filtered rows, so pages 2+ are skipped whenever page 1 has none | Critical (all AcclaimWeb counties: Chelan, Douglas) |
| D4 | No completeness check: rows read are never compared with the portal's "of N" | Partial reads report success | High |
| D5 | `NOD` in Chelan means **NOTICE OF DISCONTINUANCE** (a cancelled foreclosure), but it is mapped to pre-foreclosure "Notice of Default" | Chelan doc-type list (109 types): `123 NOTICE OF DISCONTINUANCE (NOD)`. The guard matches the text "DISCONTINU", but the grid shows only the code | Critical (cancelled foreclosures would be delivered as leads) |
| D6 | Lis pendens never captured: the grid shows the code `LP`, and the map has only "LIS PENDENS" | `190 LIS PENDENS (LP)` | High |
| D7 | Probate keyword `AFFD` = generic AFFIDAVIT; `DEATH` substring-matches TOD deeds (living owners) | Chelan list; the 10/02 AFFD hit was a generic affidavit | High (non-probate rows sold as probate) |
| D8 | Owner-name address lookup in the template posts without `searchType`, so the portal answers 302 and every lookup fails silently. The hardened `batch_lookup_pacs_by_name` never runs for Chelan (`assessor_url` NULL, no `KNOWN_ASSESSOR_URLS` entry) | Live: legacy post → 302; hardened → valid answer | High (0/2 and 0/4 addresses) |
| D9 | In single-date mode the loop never searches `date_to` itself | `while chunk_start < end` searches the chunk start only | Medium |

| D10 | Chelan table fallbacks need ≥2 body rows, so a one-record day or final page disappears (Codex) | `acclaimweb.py` DOM fallbacks | High |

### Codex pre-build consult (2026-11-12, read-only) — reconciled
- Agrees with D1–D6, D8, D9. Correction to D1: rows keep the grid's own recording date, so the damage is missed and out-of-window rows, not wrong dates. Correction to D7: `DEATH` is a whole-word match, but it still matches TRANSFER ON DEATH; the TOD gate treats NULL as allowed.
- D1 fix: detect the grid kind and wait for a fresh binding (Telerik for Chelan, Kendo for Douglas), not element replacement. A timeout raises.
- Completeness: count raw rows traversed before any filtering, against the portal total. A repeated page, missing total, or the 50-page cap fails the run; a verified fresh zero passes.
- Put the Chelan pre-foreclosure vocabulary in its own `doc_types.py` entry, keep effective per-county maps in the template, and make the partition tests compare effective maps.
- D8: use `PACS_SITES` cid=91 and delete the Chelan legacy in-template lookup. Keep Douglas's TaxSifter path. No-address rows are saved before enrichment (verified by Codex).
- Historical reconciliation: **not needed for Chelan**. Production holds 0 Chelan result rows, so there are no delivered or billed rows to re-key.

## Sources (research agent + live reads)

| Type | Official source | Access | Verdict |
|---|---|---|---|
| probate | Recorder (AcclaimWeb): `DC`, `TOD` (living owner, opt-in), no letters/probate doc types. The Superior Court Clerk uses OnBase on paid subscription; Odyssey coverage of Chelan is unconfirmed | Recorder: public disclaimer; court: paid subscriber or in person | Recorder signal only (DC). Court probate is **blocked by source access** |
| pre_foreclosure | AcclaimWeb `NTS`, `NTSCOM`, `LP` (exclude `NOD` = discontinuance) | Public disclaimer; name index 1970+, parcel 1997+; "Released through 08/10/2026" banner | Buildable after D1–D6 |
| trustee_sale | AcclaimWeb `NTS`. The sale date lives only in the document image; no newspaper NTS source was found | Index is public; images not yet checked | **Partial**: index yes, sale date needs the image (OCR). Owner decision |
| tax_delinquent | Treasurer: foreclosure list "mid-November" on Public Surplus, Dec 4–28 2026 sale; PACS `cid=90` = the Treasurer view (per-parcel). No published delinquency roll | Public; list format not yet seen | **Partial / research**: foreclosure-stage list only |
| code_violation | County CDD: complaint form only; SmartGov permit portal (code cases unchecked); 5 cities unchecked | No case-level data found | **Blocked by source** unless SmartGov exposes cases |
| divorce | No recorder doc type; court OnBase subscription | Paid / in person | **Blocked by source access** |
| death_certificate | AcclaimWeb `DEATH CERTIFICATE (DC)` recordings (recorded to clear title). DOH certificates are restricted (RCW 70.58A) | Recorder public | **Buildable** from the recorder index (same template) |

Licensing: the Assessor's search page cites RCW 42.56.070(9) (no commercial lists). The owner cleared Chelan for mailing on 2026-10-02
(`COUNTY_GIS_RESTRICTED_MAILING_ENABLED`). The Assessor also states the taxpayer may not be the legal owner, so the owner role must be kept.

## Plan (each phase ≤5 files; stop for owner approval between phases)

### Phase 2a: read completeness + Chelan vocabulary (DONE, Codex GO after 4 rounds)
- [x] Read every search from its own responses (D1, D2, D3, D4, D10). The grid DOM/pager code is deleted. Both portals load rows from POST `/Search/GridResults` (Chelan: paged `{data,total}`; Douglas: one unpaged `{Data,Total}`). Only requests issued after our click count. Zero comes only from a 200 shell with no grid binding plus the empty marker. Rows must equal total, be distinct, and carry the searched day. Network/HTTP/timeout errors raise `TransientScrapeError` (re-run); count/schema/date errors raise permanent
- [x] Single-date mode searches `date_to` too and steps one day (D9). Search-form navigation gets a 30 s deadline + 1 retry (a pre-existing Douglas flake)
- [x] `ChelanAcclaimWebScraper` (routed by exact host): pre_foreclosure `NTS`/`NTSCOM`/`LP` (NOD = discontinuance, excluded); probate `DC`/`TOD` (AFFD excluded); `DC` classified as a death in `probate.py`. `doc_types.py` Chelan tokens partition that map
- [x] Tests: 32 in `tests/test_acclaimweb_chelan.py` on real captured Chelan (43 rows, 4 pages) and Douglas responses (person names redacted). The late-answer test fails on a mutant without request correlation
- [x] Verify: affected suites pass (117), ruff clean. Main and the branch fail the same 40+19 local tests for environment reasons (no Redis). Live: Chelan 10 days and Douglas 6 days, rows read == portal total, AFNs continuous across days

### Phase 2b: property linkage (D8) (DONE, Codex GO after 3 rounds)
- [x] Each Chelan lead reads the APN from its own recording's detail page (`/details/documentdetails/{TransactionItemId}/1/1/11`, "DocLegal: APN 232020860053 …"), via an in-page fetch so the context SSRF guard applies. Outcomes: one 12-digit APN -> `found` (parcel_id); several or an odd shape -> `ambiguous` (`apn_candidates`, held for review); no legal (death certificates) -> `none`. An unreadable page after one retry raises Transient/Blocked before save, so no row is half-read. One request per recording, paced 1.5 s
- [x] The broken in-template PACS name lookup is deleted (Douglas keeps TaxSifter). `KNOWN_ASSESSOR_URLS["chelan_WA"]` = Assessor PACS cid=91, so the hardened, paced, license-gated post-save name fallback runs. `enrich.name_lookup_eligible`: a row whose recording was read is name-searched only when the recording names no parcel (`none`), so a name match never overrides recorded parcel evidence
- [x] Live (read-only, Oct 1–12): 9 leads -> 6 one-APN `found` -> **6/6 statewide-GIS situs, 6/6 PACS owner mailing** (before: 0/9 addresses). 2 TODs `ambiguous` (several parcels), 1 DC `none` -> name fallback `no_match` (stays undelivered)
- [x] Tests: 59 in the Chelan/AcclaimWeb files (APN parse incl. `&amp;`/`;`/repeat-label/plat numbers, shape, retry, refusal, unreadable-200, one-request-per-recording, name-fallback rule)

### Phase 3: death_certificate for Chelan (recorder `DC`). Code only, connector change waits for owner approval
### Phase 4 (owner decisions): trustee_sale sale-date via NTS images; tax_delinquent from the Treasurer foreclosure list; SmartGov code cases
### Blocked: divorce, court probate (paid subscription / in person)

## Review
(to fill in)
