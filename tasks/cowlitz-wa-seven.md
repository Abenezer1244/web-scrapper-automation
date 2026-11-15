# Cowlitz County, WA — seven record types (branch `feat/cowlitz-wa-seven-record-types`)

Worktree `C:\Users\micha\bl-wt\cowlitz-7`, base origin/main c8fcfecb. Discovery 2026-11-15: read-only
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
- [ ] **Phase 3 — trustee_sale** (recorder NTS + Discontinuance, image fetch + existing OCR + existing
  review flow): first verify the free page-image endpoint; never invent a sale date or amount.
- [ ] **Phase 4 — tax_delinquent** only if the owner approves the source (see questions).
- [ ] Codex review per record type (7, incl. blocker evidence for divorce / code_violation) + final
  adversarial review. Full suite on a disposable local DB. Playwright QA (Playwright, not Claude in Chrome).
- [ ] Recovery (needs approval): re-enrich the 27 short-parcel leads already in production.

Nothing is pushed, merged, deployed, or enabled without owner approval. Connectors for new types are
seeded INACTIVE.
