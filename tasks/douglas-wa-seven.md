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
- [ ] **Phase 3: death_certificate** connector type for Douglas (seeded inactive), tests.
- [ ] **Phase 4: trustee_sale** via the Chelan OCR pipeline (`nts_recorder_documents`, recorder crawler, operator review)
  generalised to Douglas NTS (33/yr). Check image access is free first. Owner decision (same as Chelan's).
- [ ] **Phase 5: tax_delinquent** — owner decision between (a) OCR of the annual Notice and Summons (foreclosure-stage only),
  (b) a Public Records Request for the Treasurer's year-round delinquent file, (c) per-parcel TaxSifter balances.
- Blocked (no code): **code_violation** (no case-level source), **divorce** (no recorder type in use; court index is
  case-only/paid). **death_certificate** certificates themselves are restricted; recorder DC is the feed.

## Seven-category matrix (Phase 1)

| Category | Source | Access | 12-mo volume | Status |
|---|---|---|---|---|
| probate | Recorder LACKPRO/DC/AFFIDE/WILL (+TOD opt-in) | public | ~74 + 70 TOD | Existing; defects G1, G3; baseline unmeasured |
| pre_foreclosure | Recorder NTS/LP | public | 36 | Existing; defects G1, G2; baseline unmeasured |
| trustee_sale | Recorder NTS (sale date in image) | index public; images unchecked | 33 | Not implemented (Phase 4) |
| tax_delinquent | Treasurer summons PDF / FTP file / TaxSifter | PDF public; FTP via PRR | unknown | Not implemented, owner decision |
| code_violation | none found | - | 0 | Blocked by source access |
| divorce | Odyssey index / JIS-Link | case index / paid | 0 recorded | Blocked by source access |
| death_certificate | Recorder DC | public | 21 | Not implemented (Phase 3) |

## Review
(to be written at the end)
