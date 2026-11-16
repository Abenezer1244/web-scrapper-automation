# Cowlitz County, WA — seven record types (branch `feat/cowlitz-wa-seven-record-types`)

Worktree `C:\Users\micha\bl-wt\cowlitz-7`, started from origin/main c8fcfecb; origin/main (head 163) merged
in at a0222a63, Cowlitz migrations renumbered 164-166. Discovery 2026-11-15: read-only
production SQL, public county pages, bounded read-only recorder searches (6 searches, paced), no logins,
nothing bought, no Tracerfy, nothing written to production.

## Baseline (production, read-only, 2026-11-15; period = all time, 2026-09-10 to 2026-10-12)

Connector `edfee01c`: Laserfiche WebLink `cowlitzinfo.net/WLAudPublic`, template mode, [probate,
pre_foreclosure], active, healthy, `assessor_url` NULL. Only 3 jobs ever ran.

| type | jobs | done | failed | records_found NULL | found | saved/billed | already delivered | no party | no parcel | no property addr | no mailing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| probate | 2 | 2 | 0 | 1 (2026-09-10, predates column) | 52 | 30 | 17 | 0/76 | 0/76 | 26/76 | 24/76 |
| pre_foreclosure | 1 | 1 | 0 | 0 | 12 | 4 | 0 | 0/12 | 0/12 | 8/12 | 8/12 |

Probate rows carry Death Certificate, Death Certificate to Perfect Death Deed, Transfer on Death Deed,
Affidavit Lack of Probate (death certificates are mis-filed under probate).

## Defects found (verified)

1. **Short parcels never enriched (root cause of every missing address).** `county_gis.py` skips any
   parcel shorter than 6 chars (lines 853, 1414, 1524). Cowlitz parcels are often 5 chars ("03110",
   "21044"); the county's own GIS layer has situs + mailing for all of them (checked 8/8). Unique leads:
   short parcel 27/27 no address; 6+ chars 1/35 no address (that one is a rural no-situs parcel).
2. **Silent partial capture.** Live 10/15-11/14/2026: portal reported 1,428 results, template captured
   1,068 (75%) and returned them as complete. The virtual-scroll loop stops after 8 empty scrolls or a
   bottom sweep and only logs. AFNs are sequential (span 1,425 ≈ 1,428), so completeness is provable.
3. **`_extract_page` swallows every exception** and returns [] (reads as zero rows).
4. **Multi-parcel documents**: the parcel cell can stack several parcels (NTS 3799745: 61896 + 6190401);
   we keep one. Must keep all and never auto-pick.
5. Death certificates live in the probate keyword map; no separate `death_certificate` type.
6. **The results grid shows only the FIRST value of multi-valued fields.** NTS 3799745's entry
   properties list 4 grantees (APLET TIFFANY, FIDELITY NATIONAL TITLE, GUILD MORTGAGE, MERS) and 2
   parcels (61896, 6190401); the grid shows one of each. An NTS whose first grantee is a lender/title
   company hides the borrower and is dropped (5 of 12 NTS in 10/15-11/14 have a company first grantee).
   Fix: read the full metadata pane (row click) for documents of the active type only.
7. Capture is complete on small windows (single days: 97/97, 74/74) and short only on large ones
   (30 days: 1,068/1,428), so split windows until each one is provably complete.

## Source feasibility (live 30-day window 10/15-11/14/2026 unless noted)

| Type | Source | Volume | Status | Why |
|---|---|---|---|---|
| probate | Recorder: Affidavit Lack of Probate, TOD deed (living-owner gate) | 10 + 19 / 30 d | Fix + keep | Court probate needs paid Odyssey (see divorce) |
| pre_foreclosure | Recorder: Notice Of Trustees Sale, Lis Pendens | 12 + 1 / 30 d | Fix + keep | Discontinuance already dropped as cancellation |
| death_certificate | Recorder: Death Certificate (+ "to Perfect Death Deed") | 2 / 30 d | Build (map split) | Recorded public instrument, not a vital record. HHS closed certificates to the public Jan 2021 |
| trustee_sale | Recorder NTS document image + existing OCR (`nts_recorder_ocr.py`, Chelan) ; Discont Of Trustees Sale cancels | 12 NTS + 3 discont / 30 d | Build, pending image-access check | Sale date/amount only in the document. Viewer offers 3 pages; plain-text view returned no text, so OCR route |
| tax_delinquent | Treasurer 2026 Certificate of Delinquency (13 parcels, 2023-or-earlier taxes, judgment 10/2/2026, auction 12/7/2026 PublicSurplus) | 13 / year | Owner decision | GIS TAX_STATUS blank on all 57,987 parcels. Per-parcel property app carries an RCW 42.56.070(9) commercial-list acknowledgement |
| divorce | Superior Court via Odyssey Portal (clerk-issued paid subscription + agreement), JIS-Link (paid, display-only) ; recorder: 0 dissolution docs in 30 d | n/a | Blocked by source access | GR 31 bulk / commercial solicitation limits; nothing bought |
| code_violation | County Nuisance Abatement (complaint form only); Accela portal has no enforcement module; Longview, Kelso, Kalama: complaint intake only; Castle Rock, Woodland: none found | n/a | Blocked by source access | No public case-level index anywhere |

## Owner decisions 2026-11-15
Run Phases 1-3 in a row (Codex review per phase). Build tax_delinquent from the 13-parcel list.

## Codex pre-build consult (2026-11-15, read-only) — reconciled, all adopted
- Completeness gate = distinct RAW portal rows == "N Results", counted before type/party/parcel
  filtering; parse comma totals; AFN span is supporting evidence only. Split windows until complete;
  a one-day window still short raises. Return [] only for a verified zero or a complete capture.
- Short parcels: county-query minimum length (default 6, Cowlitz 5) at the county query only; the
  statewide guards stay at 6.
- Multi-parcel docs: `parcel_id=None`, `recording_apn_lookup="ambiguous"`, `apn_candidates=[...]`
  (AcclaimWeb pattern), retained but never delivered or billed; no concatenation.
- death_certificate: reuse `orient_probate_party`; migration like `157_chelan_death_certificate.py`.
  Appending a type to the ACTIVE recorder row makes it live at deploy; deploy stays owner-gated.
- trustee_sale: generalize Chelan's OCR staging (APN extraction + Chelan-only review guard);
  discontinuance must durably cancel earlier NTS, incl. ones outside the window.
- tax_delinquent: an annual certificate does not prove continuing delinquency; need fresh
  redemption evidence and expire auction claims after 12/7/2026.

## Plan (phases, <=5 files each, approval between phases)

- [x] **Codex pre-build consult** on this plan (before any code); reconciled above.
- [x] **Phase 1 — done 2026-11-15 (Codex r4 GO; live 1,428/1,428 both types).** Built: per-day searches
  with exact count check (bisecting was slow: 8-day windows still short, 339/399); DocView
  properties parsed from `#metadataTable` (BeautifulSoup), stable-read + AFN + grid-row
  cross-check, else raise; NTS borrower from grantees; primary person + same-surname
  co-parties only (others to heirs: live NTS listed 6 person-like grantees, an LP listed HUD);
  multi-parcel -> ambiguous; `gis_min_parcel_len()` used by every generic GIS gate
  (enrich.py x3, mailing recovery), King/Pierce gates untouched. Live 10/15-11/14 (pre-final
  code): 1,428/1,428 raw rows; 12 pre_foreclosure leads (11 NTS + 1 LP) vs the 7 the grid
  allowed; 3 ambiguous. Codex r1 NO-GO (worker 6-char gate, DocView completeness, tests):
  all fixed. Tests: 33 template + 3 worker-level; negative controls fail on old code.
  Open item for owner: owner-on-title display (`assessor_current_owner`) from the GIS deed
  holder is King-only today; Cowlitz mailing depends on COUNTY_GIS_RESTRICTED_MAILING_ENABLED.
  Original scope (`templates/laserfiche_weblink.py`,
  `enrichment/county_gis.py`, tests): capture every row (root-cause the 75% capture; split the window
  if needed) and prove it per window (count == total, AFN coverage), else raise; `_extract_page`
  errors raise; keep all parcels of a multi-parcel doc, no auto-pick; per-county minimum parcel length
  (Cowlitz 5). Offline fixtures from saved live HTML. Live re-proof on one bounded window.
- [x] **Phase 2 — done 2026-11-15 (Codex GO; one Low added as scrape-level tests).** Map split
  (probate no longer carries certificates), death_certificate routed through orient_probate_party,
  shared agency regexes accept the recorder's "DEPARMENT" (1 prod Cowlitz probate lead named the
  agency as decedent), migration 162 verified up / re-run / down / up on the local DB. Billing
  identity is record-type-free (tasks.py:1251/1477), so reclassifying never re-bills. Live
  10/15-11/14: 1,428/1,428 rows, 4 certificates -> 3 leads (2 found, 1 ambiguous with 6 parcels;
  1 with no parcel dropped). Original scope:
  **Phase 2 — death_certificate** (template map split, doc scope/registry, migration adding the type
  to connector `edfee01c`): decedent = grantee when the State is grantor; parcel required.
- [x] **Phase 3 — trustee_sale (auction leads), done.** 3a `10c27137`: the OCR child reads Laserfiche's
  CCITT Group 4 "Print (as PDF)" (bytes wrapped as TIFF, never decoded in Python; PDF-token drawing
  grammar after a Codex High). 3b `6d1d6304`: Chelan's recorded-NTS review pipeline made per county;
  Cowlitz discovery (NTTRUS / DISTRS from entry properties), fetch_pdf (browser download cancelled,
  URL re-read through safe_download_to_file, same-origin), crawl staging each notice inside one budget,
  reach-back + fail-loud gap; shared fix: deferred retries count as unread (Chelan too). 3c `d840718f`:
  reader, NTS_MATCH_COUNTIES, beat 09:00 UTC, connector INACTIVE (now migration 165). Live 11/03-11/13:
  13 recordings, 9 PDFs OCR'd; OCR unaided read the amount 9/9, the sale date 4/9 (operator confirms
  every field against the image). 2 of 9 notices have no parcel in the index: not approvable.
  Codex: 3a r3 GO, 3b r3 GO, 3c GO.
- [x] **Phase 4 — tax_delinquent, done** `60dc1370` + `8bce696e`: the Treasurer's live listing (13
  certified, 7 listed, updated 11/5) reconciled with Count and title-report links; holder must equal
  the Assessor's DEED_HOLDER_NAME (live 7/7); stale (>45 d), future-dated or other-cycle listings fail;
  past a (tentative) sale date the run fails ("outcome not published"); amount = minimum bid with its
  basis; bill_year None (validator exception for this source only); status "tax_foreclosure_sale_tentative".
  Connector INACTIVE (migration 166). Codex r1 NO-GO, r2 GO for merge inactive.
- [x] Codex: pre-build consult; per phase (every record type, incl. blocker evidence); final
  adversarial review r1 NO-GO (stale base; migration collision), after merging main r2 **GO for merge
  and deploy with 165/166 inactive**.
- [x] Full suite on the Phase 1-3a head: 8,825 passed, 54 skipped (49 min). Merged head: see review.
- [ ] Playwright UI QA: not done (the new types are not deployed anywhere a UI can reach).
- [ ] Recovery (needs approval): re-enrich the 27 short-parcel leads already in production; fix the
  1 production Cowlitz probate lead whose party is the State agency.

## Readiness matrix (2026-11-15)

| Type | Source | Status | Remaining before customers |
|---|---|---|---|
| probate | Recorder (Affidavit Lack of Probate, TOD deed) | Implemented, live-verified, awaiting deploy | deploy; recovery of 19 short-parcel leads |
| pre_foreclosure | Recorder (NTS, Lis Pendens) | Implemented, live-verified, awaiting deploy | deploy; recovery of 8 short-parcel leads |
| death_certificate | Recorder (recorded Death Certificate) | Implemented, live-verified; migration 164 turns it on at deploy | deploy |
| trustee_sale | Recorder NTS images, OCR + operator review | Implemented, connector INACTIVE (165) | deploy, first crawl, operator reviews, then activate |
| tax_delinquent | Treasurer's live foreclosure listing | Implemented, connector INACTIVE (166) | show the minimum-bid basis in exports/UI, then activate before 12/7/2026 |
| divorce | Superior Court (Odyssey, paid + GR 31) | Blocked by source access / legal | none planned |
| code_violation | none: complaint intake only (county + 5 cities) | Blocked by source access | none planned |

## Review (2026-11-15)
- The two live scrapers were silently incomplete (75% of rows; grid hid co-grantees and extra
  parcels) and 5-character parcels never reached any GIS gate. All three are fixed at the root.
- Auction leads reuse Chelan's reviewed pipeline instead of a new one; the refactor kept Chelan's
  behaviour (its suites pass) except the stricter "deferred retries are unread" rule.
- Deploy prerequisites (Codex final): migrate to 166 and check the Cowlitz rows (recorder +
  death_certificate; 165/166 inactive); deploy worker + beat together and confirm Chromium,
  Tesseract (eng), Redis locking and recorder alerts in the image; watch the first Cowlitz crawl;
  before activating 165 review the first notices; before activating 166 expose the minimum-bid basis.
- Follow-ups: Chelan's pre-existing silent 60-day reach clamp; cross-type identity of recorder vs
  approved-notice leads for the same property (all counties); Cowlitz deed-holder mailing depends on
  COUNTY_GIS_RESTRICTED_MAILING_ENABLED (owner/counsel).

Nothing is pushed, merged, deployed, or enabled without owner approval. Connectors for new types are
seeded INACTIVE.
