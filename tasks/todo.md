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

### Phase A — safe_http size-capped download + settings  ✅ (commit ae8e61b)
- [x] `settings.py` + `.env.example`: `MAX_DOWNLOAD_BYTES` default 100 MB (104857600) — Codex-lowered from 250.
- [x] `safe_http.py`: `safe_download_to_file()` per-hop validate, stream, byte-cap abort, assert 200 + non-empty;
      cap logic extracted to pure `_stream_capped()`.
- [x] Tests in `test_safe_http.py` (mirrors src): 11 new — SSRF/https/cap-arg guards + `_stream_capped` real-I/O.

### Phase B — Snohomish scraper  ✅ (commit 8fc1c12)
- [x] `snohomish_wa_tax_delinquent.py` — pure-HTTP scraper; landing-link resolver (excludes desc twin);
      capped temp download + finally-cleanup; stream-parse; filter (14-digit + year<as_of + owed>0);
      per-parcel aggregate (sum owed, min year); year-level enrichment detail; structural-validation canary.
- [x] `tests/test_snohomish_tax.py` — 8 tests on REAL captured rows: multi-year aggregation, exclusions,
      malformed counting, link selection + id-rotation + no-link-raises.

### Phase C — wire-up: source gate + registry + migration  ✅ (commit 34b06b8)
- [x] `tasks.py` — `_extract_tax_fields` gate → `_TRUSTED_TAX_SOURCES` frozenset (King + Snohomish).
- [x] `registry.py` — module added to `_ALLOWED_SCRAPER_MODULES`.
- [x] `alembic/versions/040_*.py` — idempotent `county_connectors` INSERT; base_url = stable landing page.
- [x] +4 gate tests (Snohomish string-amount/int-year trusted; lookalike source ignored).

### Phase D — verify + Codex review + ship
- [x] py_compile / ruff (my files clean; tasks.py+registry.py pre-existing errors = on main, out of scope) /
      pytest (50 touched tests green; full suite collects 334, no import breakage).
- [~] Security Master Review (§14) on the diff — self-review below.
- [~] **Codex review the diff** (`codex review --base main`) — RUNNING. Critical/High from either = NO-GO.
- [ ] Live Railway smoke (scrape Snohomish tax_delinquent, confirm rows + delinquent_amount populated) — needs deploy.
- [ ] Merge to main (migration 040 deploy-order note), update BUILD_JOURNAL + memory.

## Security self-review (Master §14, BridgeLeads non-negotiables)
- **SSRF:** download host fixed county-gov (`add_scrape_domain` at module top + base_url host seeds allowlist);
  `safe_download_to_file` revalidates EVERY hop (`resolve=True`), `require_allowlisted=True`, `require_https=True`,
  refuses scheme downgrade; landing fetch via `safe_get(require_allowlisted=True)`. ✅
- **DoS/OOM:** hard byte cap (`MAX_DOWNLOAD_BYTES`) + early Content-Length reject + stream-to-disk + per-parcel
  aggregate (never 325K rows in RAM). ✅
- **CSV injection:** owner/situs/mailing → first-class `ScrapedRecord` cols (export `sanitize_for_csv` covers);
  nothing surfaced raw from `enrichment_data`. ✅
- **Tenant isolation:** no new queries; insert path keeps existing `user_id=job.user_id`; source-gate purely
  transforms enrichment_data. ✅
- **Source-gate trust:** frozenset exact-match; lookalike/untrusted sources ignored (tested); bounds/Decimal intact. ✅
- **Silent-empty / wrong-file:** structural validation (17-field, malformed-ratio) + zero-parcel canary → FAIL loudly,
  no silent-swallow. ✅
- **Secrets:** none added (public county data, no auth). ✅
- **Error leakage:** failures raise clean `RuntimeError`/`ValueError` (operator messages, no raw URL/stack to client;
  worker FAILED path attaches reference id as today). ✅
- **TCPA/DNC:** scraper emits no phones; `phone_dnc_flag` stays NULL → excluded from default dialer-ready set. ✅

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

# Thread 3 — Dialer connectors (full build, user-approved). Codex design consult done (session 019e9b22 follow-up).
Codex raised the bar: per-contact OUTBOX is required (PhoneBurner has no bulk endpoint → 500 POSTs →
partial-success silent loss). Refined scope to bound blast radius:
- **Phase A ✅ (c0943d4):** connector seam (ABC + GenericWebhookConnector byte-identical + dialer_type
  discriminator + sweep dispatch). 7 tests. No transport change.
- **Phase B (vendor-only outbox — generic path UNTOUCHED):** `dialer_delivery` outbox table (migration 041:
  id, job_id, result_id, user_id, scraper_config_id, vendor_id, status[pending|delivered|failed],
  attempts, last_error, vendor_response_code, vendor_contact_id, created_at, delivered_at). Sweep: for a
  VENDOR dialer_type, claim job → INSERT one outbox row per lead → enqueue chunked processor; GENERIC stays
  on the existing deliver_job_webhook path (no billing-path change). New `process_dialer_outbox` task:
  re-reads config from DB (owner-match ScraperConfig.user_id==Job.user_id + Result.user_id), builds vendor
  request via connector, POST host-allowlisted + response redacted, updates per-row status/last_error;
  creds built at send time (never a task arg). Replay endpoint (user_id-scoped) resets failed→pending.
  NOTE: generic catch-hook-URL-in-args is PRE-EXISTING status quo, not regressed; hardening it = documented follow-up.
- **Phase C:** PhoneBurner connector (contact-creation ONLY, host allowlist www.phoneburner.com, OAuth
  Bearer + owner_id from deliver config, extra=forbid + token validators). Live smoke needs user creds.

## Review — Thread 1 (Snohomish) SHIPPED + LIVE ✅
- Merged to main (`9a70bab`), pushed, deployed — health 200 (migration 040 applied on boot).
- **Live smoke against the real source:** 44.7 MB / 325,043 rows / 0 malformed → 10,548 delinquent rows →
  **4,269 parcels, all with delinquent_amount + bill_year**, $16.3 M total owed. Multi-year aggregation
  confirmed (VERIZON 2023+2024+2025 = $2,376.01). ZERO API/UI/migration-column change.
- Codex consult (pre-build) + Codex diff review (1 P2 found + fixed, no Critical/High). 58 tests, ruff-clean.
- Prod-API connector check blocked by permission classifier (not in deploy scope) — health-200 +
  idempotent migration + live smoke stand as proof.

## Next threads (2 & 3)
- **Thread 2 — DNC scrubbing:** BLOCKED-ON-DECISION (legal/vendor). Needs: can BridgeLeads scrub the
  federal DNC registry + pass the flag to customers, or is that the customer's SAN/responsibility? Which
  vendor (DNC.com / Contact Center Compliance / etc.) + budget? No real DNC source ⇒ nothing to build
  (no-mock rule). Surfaced to user.
- **Thread 3 — native dialer connectors:** research done, DEMAND-GATED. Smallest useful step = the
  DialerConnector abstraction seam + ONE reference connector, built when a paying customer names a dialer.
