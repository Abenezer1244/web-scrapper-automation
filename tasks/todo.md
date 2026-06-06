# Post-Milestone Build — Snohomish Tax Scraper (Thread 1 of 3)

Direction (user): do all 3 post-milestone threads **one-by-one**, **Codex-verifies each**,
via a **dynamic workflow**, **security is priority**.
Order: (1) Snohomish tax scraper → (2) DNC scrubbing → (3) native dialer connectors.

## Research + security review — DONE (dynamic workflow `wf_0e4598c4-344`, salvaged)
- Snohomish research + adversarial security review COMPLETE (verdict GO-WITH-FIXES).
- Native-dialer-connectors research + security COMPLETE (thread 3, parked).
- DNC research agent ran away (1h45m) → killed; DNC was predicted blocked-on-decision anyway (thread 2).

## LIVE FILE INVESTIGATION — DONE (the required precondition)
Source: Snohomish "Current Tax List" — `…/DocumentCenter/View/149173/snohomish_tax_data_totals`
(linked off `…/5568/Treasurer-Public-Records`, updated monthly, **doc-ID rotates**).
- **Pipe-delimited `.txt`, NO header row, 17 cols, 325,043 rows, 44.7 MB**, UTF-8 BOM, `\r\n`.
- **No HTTP redirect** (direct 200) — disproves the security review's "302" High.
- Columns: `0`=parcel/account, **`1`=tax/bill YEAR**, `2`=situs addr, `4`=situs city, `5`=st, `6`=zip,
  `7`=owner, `10/11/12`=mailing city/st/zip, `13`=as-of date, `14`=total annual,
  `15`=half installment, **`16`=amount owed/balance**.
- `parcel len`: 304,477 are **14-digit real property** (target) + 20,566 7-digit personal-property (exclude).
- **Delinquent set = 14-digit parcel AND `year < current` AND `owed(col16) > 0` → 10,548 accounts.**
  col16==col15 for all 10,548; col16==col14 for 8,948. Amounts already clean numerics (no `$`/commas).
- A parcel can recur across years → **aggregate per parcel: sum owed, MIN(year)=oldest=most months delinquent.**

## Mapping to existing Phase 4 infra (ZERO API/UI/migration-column change)
- `delinquent_amount` ← sum(col16) per parcel
- `delinquent_bill_year` ← min(col1) per parcel (true tax year, King Jan-1 semantic family → months filter works)
- `party_name` ← owner (col7); `property_address` ← situs (col2 + city/st/zip); `mailing_address` ← mailing
- `enrichment_data.source` = `"snohomish_county_delinquent_taxes"` (gates `_extract_tax_fields`)

## Security fixes folded in (from adversarial review + live facts)
- [HIGH-confirmed] **44.7 MB download → worker OOM.** Add size-capped STREAMING download helper to
  `safe_http.py` (stream=True, per-hop SSRF revalidate, abort > `Settings.MAX_DOWNLOAD_BYTES`), write to
  temp file, parse line-by-line, filter delinquent in the loop. NEVER materialize 325K rows in RAM.
- [HIGH-downgraded] redirect → none live, but helper still follows+revalidates per hop (future-proof).
- [HIGH-resolved] months semantic → real bill-year col exists; populate directly, do NOT synthesize from CoD PDFs.
- [MED] **doc-ID rotation** → connector base_url = stable landing page; scraper parses the current
  "Current Tax List" link (exclude the "description of the fields" anchor) before download.
- [MED] **canary** → 0 delinquent rows parsed ⇒ raise (job FAILS loudly), never silent-empty.
- [LOW] all human fields → first-class `ScrapedRecord` cols (exporter `sanitize_for_csv`); none raw from enrichment_data.
- [LOW] errors → reference-id/clean operator message on download/parse failure; no silent-swallow (the
  `_run_inline_enrichment` landmine); fail loudly.
- SSRF allowlist: `add_scrape_domain("www.snohomishcountywa.gov")` at module top (worker importlib picks it up).

## Plan (phased, ≤5 files/phase, TDD, verify each)

### Phase A — safe_http size-capped download + settings  (3 files)
- [ ] `src/config/settings.py` + `.env.example`: add `MAX_DOWNLOAD_BYTES` (default 262144000 = 250 MB).
- [ ] `src/utils/safe_http.py`: add `safe_download_to_file(url, dest, *, max_bytes, require_allowlisted,
      require_https=True, follow_redirects=True, ...)` — per-hop validate, stream, byte-cap abort+raise,
      assert 200 + non-empty.
- [ ] `tests/test_safe_http_download.py` — cap enforcement, non-200 raise, empty raise (real local temp I/O).

### Phase B — Snohomish scraper  (1 file + tests)
- [ ] `src/scrapers/snohomish_wa_tax_delinquent.py` — `SnohomishWATaxDelinquentScraper(BridgeScraper)`:
      module-top `add_scrape_domain`, landing-page link discovery, capped download to temp,
      stream-parse pipe rows, filter (14-digit + year<current + owed>0), aggregate per parcel,
      emit ScrapedRecord with source-tagged enrichment_data. Canary raise on 0 rows.
- [ ] `tests/test_snohomish_tax.py` — real fixture (slice of the live file in repo), parse/aggregate/filter,
      CSV-injection owner (`=cmd`) neutralized, `_extract_tax_fields` returns non-None Decimal+year.

### Phase C — wire-up: source gate + registry + migration  (3 files)
- [ ] `src/workers/tasks.py` — widen `_extract_tax_fields` gate to a frozenset of trusted sources
      (add `snohomish_county_delinquent_taxes`).
- [ ] `src/scrapers/registry.py` — add module to `_ALLOWED_SCRAPER_MODULES`.
- [ ] `alembic/versions/040_*.py` — INSERT `county_connectors` row (snohomish/wa/tax_delinquent, manual,
      base_url = stable landing page). Idempotent guard.

### Phase D — verify + Codex review + ship
- [ ] `python -m py_compile` / ruff / pytest (no-DB tests green).
- [ ] Security Master Review (§14) on the diff.
- [ ] **Codex review the diff** (review + challenge). Critical/High from either = NO-GO.
- [ ] Live Railway smoke (scrape Snohomish tax_delinquent, confirm rows + delinquent_amount populated).
- [ ] Merge to main (migration 040 deploy-order note), update BUILD_JOURNAL + memory.

## Pre-code gate
- [x] **Consult Codex on this approach** (session `019e9b22…`) — DONE. Approach sound, no architectural change.
  Reconciled refinements folded in (all adopted):
  - **Structural validation (not just zero-row canary):** expect 17 pipe-fields/row, col1 = 4-digit year;
    track malformed-row count, FAIL if malformed-rate high OR expected structure missing → catches the
    "county swapped the file, we parse the WRONG file but nonzero" silent failure (Codex's #1 prod risk).
  - **Year granularity in enrichment_data:** `delinquent_years[]`, `delinquent_year_count`, `oldest_tax_year`,
    `as_of_date` (col13) — audit/debug, don't collapse to just sum+min.
  - **bill_year is APPROXIMATE** (WA halves due Apr30/Oct31, not Jan1): keep `min(year)` for King-compat,
    document as approximation (both reviewers agree it's acceptable; same semantic family as King).
  - **MAX_DOWNLOAD_BYTES default = 100 MB** (104857600), not 250 MB — 512 MB worker under concurrency.
  - **Temp file:** `NamedTemporaryFile(delete=False)` + guaranteed `finally` unlink (Windows handle care).
  - **Test matrix:** parser/aggregation fixture; landing-link selection excludes "description of the fields"
    anchor; `_extract_tax_fields` IGNORES non-allowlisted source even with tax-looking fields; end-to-end
    source-string → both columns populated. + INFO metrics (bytes, rows, malformed, delinquent, parcels, oldest yr, total $).

## Review
(to fill in at the end)
