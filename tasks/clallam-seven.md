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
- [ ] pre_foreclosure: skip discontinuance rows (raw grantee `DISCONTINU… <instrument>`); verify each NTS candidate by instrument search; drop cancelled originals (`eagleweb.py`)
- [ ] probate: exclude "WRIT OF EXEC" (`eagleweb.py`)
- [ ] tests with recorded Clallam row fixtures; pytest on a test DB; ruff
- [ ] live before/after scrape of Clallam probate + pre_foreclosure (no DB writes)
- [ ] Codex review of the diff

## Phase 2 — new types (after approval)
- [ ] death_certificate via EagleWeb `DEATH`
- [ ] tax_delinquent: `clallam_wa_tax_delinquent.py` + registry allowlist + fixture test
- [ ] per-type Codex review; connector migration written but only applied with owner approval

## Phase 3 — UI + report
- [ ] Playwright (Chromium) checks of the frontend at 320/375/390/430/tablet/desktop, light/dark
- [ ] cross-cutting Codex challenge; final matrix; BUILD_JOURNAL entry

## Review
(to fill in)
