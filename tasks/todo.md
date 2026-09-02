# Pierce auction leads "Test 3" data-quality audit (2026-09-02)

Branch: `fix/nts-matured-obligation-amount` · worktree `bridgeleads-worktrees/test3-nts-amount` off `origin/main` (`5106fe0`)

- [x] Trace the blank Default Owed end-to-end (UI → API → DB row → notice row → source page) — Case A (source has it)
- [x] Fix the section-IV amount parser + real-notice fixture + pinning tests
- [x] Fix the "Subject to"-in-parenthetical grantor truncation (parser) + read-time cleaner (cached rows)
- [x] Fix county-GIS batch keying (dashed parcels lost the county mailing address) + statewide half-address
- [x] Audit all 6 records vs source (names, parcels, addresses, auction dates, amounts) — see BUILD_JOURNAL 2026-09-02
- [x] Codex consult before code + 3 review rounds (final GATE PASS); every finding re-verified against code
- [x] Browser verification of the live page (Playwright Chromium; 6 rows, UI == API, 0 console errors)
- [x] Idempotent repair script for the historical rows (dry-run first)
- [ ] 👤 merge + deploy; wait for the 10:30 UTC NTS crawl; run the repair script dry-run → `--apply --party-names`
- [ ] 👤 `feat/fields-output-visibility` is already merged (#107/#111, reshaped by #128) — obsolete, do not re-merge

## Review
Root causes were three independent defects, none in the UI: a parser regex that required
"principal" wording, a label-stop firing inside a parenthetical, and a batch-GIS dict keyed by
the server's parcel spelling while the worker keys by the lead's. Nothing was hard-coded to
Test 3; every fix is pinned by a real-source fixture or a real ArcGIS feature.

---

# Snohomish tax_delinquent — source layout change (2026-07-30)

Branch: `chore/xcheck-2026-07-30` · worktree `bridgeleads-worktrees/xcheck-0730s2` off `origin/main` (`d56b11c`)

## Cross-check result

Swept production (`scripts/diag_build_health_sweep.py`). Jobs healthy (0 stuck, 0 failures
in 14d, 0 stranded batch runs). One genuine user-facing break found.

**snohomish/tax_delinquent connector `health_status='down'`, 1 active user config, broken ~5 weeks.**

Reproduced the exact production exception:

```
RuntimeError: Snohomish tax list format unexpected: 327721/327721 rows malformed (>20%)
  — possible source change            (snohomish_wa_tax_delinquent.py:367)
```

### Root cause
The county changed the bulk-file layout **17 → 15 pipe-delimited fields**.
`_EXPECTED_FIELDS = 17`, so 100% of rows fail the length check and the
`_MAX_MALFORMED_RATIO = 0.2` structural guard aborts. **The guard behaved correctly** —
only the layout constant and column indices are stale.

### Measured over the ENTIRE live file (327,721 rows streamed — not a sample)
- field count uniformly **15** on 327,721/327,721 rows
- parcels: 14-digit real property 307,308 · 7-digit personal 20,412 · 1 blank
- tax years 1996–2026 (2026: 310,058 · 2025: 6,287 · 2024: 3,400 · 2023: 1,868 …)
- `as_of` uniformly `20260701` (format changed from `mm/dd/yyyy` → `YYYYMMDD`)
- **8,900** 14-digit prior-year rows with owed > 0 → a corrected parser yields real leads

### Column semantics — derived, then validated at scale
Invariant `c11 == c12 + c13` holds on **327,720/327,720 rows (100.0000%, 0 failures)**
⇒ `c11` = billed-to-date, `c12` = paid, `c13` = **still owed**.
`c14` vs `c11`: equal 27,407 / ~2× 251,031 / other 9,241 ⇒ `c14` = full-year levy.
Old code took the **last** column as owed (`f[16]`); in the new layout the last column is
the levy, so a naive reindex would silently overstate every amount.

| | old (17) | new (15) |
|---|---|---|
| situs | `f2` street, `f3` line2, `f4` city, `f5` st, `f6` zip | `f2` street, `f3` city, `f4` st, `f5` zip |
| owner | `f7` | `f6` |
| mailing | `f8` line1, `f9` line2, `f10` city, `f11` st, `f12` zip | `f7` city, `f8` st, `f9` zip — **no street** |
| as_of | `f13` `mm/dd/yyyy` | `f10` `YYYYMMDD` |
| amounts | `f14` billed, `f15`, `f16` owed | `f11` billed, `f12` paid, `f13` owed, `f14` levy |

### Two further landmines found
- **(a) silent year-boundary bug.** `_as_of_year()` parses only `mm/dd/yyyy`; `20260701`
  returns `None` → silently falls back to `datetime.now(UTC).year`. Correct by luck in
  2026; a file published Dec-2026 read in Jan-2027 would use 2027 and classify the whole
  current year as delinquent. No error raised.
- **(b) city-only mailing address.** New layout has mailing city/state/zip and no street.
  `_join_address` would emit `"MONROE, WA 98272-2204"`. Verified that
  `address_intel.compute_owner_flags(property_address, mailing_address)` derives
  `owner_state` / `absentee_owner` / `out_of_state_owner` **from the mailing address** —
  a city-only value manufactures false absentee signals. Also the standing skip-trace rule:
  never stuff a city-only value into an address field.

## Codex consult (design, before any code)
Consensus on all findings. Codex positions adopted:
- **Q1** support BOTH 15 and 17 via explicit named layout maps (both URLs are live and the
  county rotates: `..._36.txt` vs `..._39.txt`); unknown/mixed field counts fail loudly.
- **Q2** `mailing_address = None`; keep city/state/zip in `enrichment_data` for audit.
- **Q3** `total_billed` stays **billed-to-date** (`c11`) so the meaning of the field on the
  2,253 existing Snohomish rows does not silently change; levy gets a new key.
- **Q4** `as_of` is structural — if it cannot be parsed, **fail**, never fall back.
- **Q5** encode the invariant as a *checked contract with diagnostics*, not a belief.

## Plan — Phase 1 (this branch, parser correctness only) — DONE, commit `0a97149`
- [x] 1. Replace `_EXPECTED_FIELDS = 17` with explicit layout maps for 15- and 17-field files
- [x] 2. Index all column reads through the selected layout; owed = layout's owed column
- [x] 3. `_as_of_year()` parses `YYYYMMDD` and `mm/dd/yyyy`; unparseable ⇒ raise, no fallback
- [x] 4. `mailing_address = None` when the row has no mailing street; locality →
       `enrichment_data.mailing_locality` (+ `full_year_levy`, `source_layout` for audit)
- [x] 5. Reject mixed/unknown field counts loudly; record chosen layout in `stats`
- [x] 6. Update/extend `tests/test_snohomish_tax.py` for both layouts + the year-boundary case
- [x] 7. `ruff` + targeted pytest; then Codex reviews the diff

## Deferred (NOT this branch)
- Codex Q4's bulk-source **contract smoke check** (sample first N rows, alert independently
  of user jobs). New subsystem touching the scheduler; overlaps the §8-gated
  external-source canary work in `HANDOFF-king-owner-names-2026-07-30.md` §9.3. Separate PR.
- `county_connectors.state` case split (`'WA'` 14 rows vs `'wa'`): **not a correctness bug** —
  every lookup is case-normalised (`registry.py:76`, `scrapers.py:113,750`, `jobs.py:133`,
  `batches.py:230`). It does break the picker's `order_by(state, county)` (non-alphabetical,
  clark listed twice) and defeats `ix_county_connectors_picker`. Cosmetic + perf, separate PR.
- **Retracted, not a bug:** 12 active+healthy connectors with empty `scraper_class` are
  `scraper_mode='ai'` and resolve via `_detect_template(base_url)` → `EagleWebScraper`.
  Verified all 24 active+healthy connectors resolve at runtime (0 broken).

## Review

**Changed** (commit `0a97149`, branch `chore/xcheck-2026-07-30`, no migration):
- `src/scrapers/snohomish_wa_tax_delinquent.py` — `_Layout` dataclass + `_LAYOUT_V15` /
  `_LAYOUT_V17` maps selected by field width and locked per file; all column reads indexed
  through the layout; `_as_of_year()` accepts `YYYYMMDD` and `mm/dd/yyyy` with month/day
  validation so a 14-digit parcel or an amount can't be read as a date; `scrape()` raises
  when the as-of year is unparseable instead of falling back to the wall clock;
  `mailing_address` gated on an actual street line in the data; `full_year_levy`,
  `mailing_locality`, `source_layout` added to `enrichment_data`; `layout` added to `stats`.
- `tests/test_snohomish_tax.py` — 13 → 29 tests.
- `scripts/diag_snoho_amount_invariant.py`, `scripts/diag_snoho_tax_canary_repro.py` — kept
  as the reproducible evidence for the column mapping and a per-connector canary repro.

**Proof (live production source, not a fixture):** 327,721 rows, 1 malformed (the file's own
leading empty record), 8,900 delinquent rows → **1,954 parcels**, `as_of_year=2026`,
`layout=v15_2026_07`, real owner names + situs addresses. "canary would set healthy."

**Tests:** 29/29 in `test_snohomish_tax.py`; **244 passed** across `-k "tax or dedup or
snohomish or lead_export or address_intel"`. `ruff` clean on all touched files.
Local rig is not isolated (handoff §7) — **CI is the authoritative gate.**

**Codex:** consulted on the design before any code (agreed on all four decisions; its
"support both layouts" and "never fall back on as_of" positions are what shipped), then
reviewed the diff — **pass, no findings, no regressions identified.**

**Deliberate behaviour change to flag:** the mailing rule also applies to the old layout.
Verified 0 of 328,069 rows in the v17 file ever populated the mailing street, so the
**2,253 existing Snohomish rows in prod carry a city-only `mailing_address`** and their
`absentee_owner` / `out_of_state_owner` flags were derived from it. New rows will have
`mailing_address = NULL` + `enrichment_data.mailing_locality`. Existing rows are NOT
backfilled by this change — a separate decision.

**Not done / open:**
1. `scraper_configs` for snohomish/tax_delinquent will not recover until the connector's
   `health_status` flips off `down`. The hourly canary samples 5 random connectors of 30,
   so it should clear on its own within a few hours of deploy — but nothing forces it.
2. The canary probes only `record_types[0]` per connector row while writing ONE
   `health_status` for the whole row (`scheduler_helpers/health.py:287`). Harmless for
   Snohomish (single-type rows) but wrong for multi-type connectors like
   king `["probate","pre_foreclosure","death_certificate"]`. Untouched here.
3. Codex Q4's bulk-source contract smoke check — the reason this sat broken 5 weeks.
   Deferred (see above); overlaps the §8-gated canary work.
