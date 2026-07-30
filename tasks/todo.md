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

## Plan — Phase 1 (this branch, parser correctness only)
- [ ] 1. Replace `_EXPECTED_FIELDS = 17` with explicit layout maps for 15- and 17-field files
- [ ] 2. Index all column reads through the selected layout; owed = layout's owed column
- [ ] 3. `_as_of_year()` parses `YYYYMMDD` and `mm/dd/yyyy`; unparseable ⇒ raise, no fallback
- [ ] 4. `mailing_address = None` when the layout has no mailing street; locality →
       `enrichment_data.mailing_locality` (+ `full_year_levy`, `layout` for audit)
- [ ] 5. Reject mixed/unknown field counts loudly; record chosen layout in `stats`
- [ ] 6. Update/extend `tests/test_snohomish_tax.py` for both layouts + the year-boundary case
- [ ] 7. `ruff` + targeted pytest; then Codex reviews the diff

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
_pending_
