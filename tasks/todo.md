# Task: Cap tax-delinquent leads at 18 months (Snohomish + King)

## Context
Admin Snohomish tax-delinquent job (`b33009fc`, 4,269 rows) surfaces parcels whose
**oldest unpaid year** (`delinquent_bill_year`, shown as "Oldest Tax Year") goes back
to 1996. By design the tax scrapers aggregate ALL unpaid prior years per parcel and
set `bill_year` = oldest year. User wants a hard **18-month** cap based on the current
year.

## Decisions (locked with user)
1. **Cap rule:** drop a parcel if its *oldest* unpaid year is >18 months old
   (only parcels fully within 18 months survive). Today that = `delinquent_bill_year >= 2025`
   → keeps ~2,253 of 4,269 rows.
2. **Existing data:** HIDE from UI/exports, do NOT delete (reversible; matches prior
   "old rows are point-in-time snapshots, fix-forward" decision).
3. **Future scrapes:** don't include >18-month parcels.

## Single source of truth
`DEFAULT_TAX_MAX_MONTHS = 18`. Predicate already exists:
`bill_year_bounds_for_months(None, 18, today)[1]` → `min_year` → keep iff
`delinquent_bill_year >= min_year`. (tax_filters.py — pure, tested.)

## Plan (phased, <=5 files/phase)

### Phase 1 — Hide existing >18mo rows on the primary surface (per-job results + CSV)
- [ ] tax_filters.py: add `DEFAULT_TAX_MAX_MONTHS = 18` + helper `default_tax_cap_condition(today)`.
- [ ] jobs.py: in the 3 tax endpoints (list `:310`, count `:669`, download `:874`),
      when job `record_type == "tax_delinquent"`, AND the default cap onto the query.
- [ ] tests: cap keeps bill_year>=min_year, drops older; non-tax jobs unaffected.
- VERIFY: pytest + re-run diag_snoho_tax_filter.py (read-only) → 4,269 visible-capped to ~2,253.

### Phase 2 — Future scrapes don't store >18mo parcels (at source)
- [ ] snohomish_wa_tax_delinquent.py `parse_tax_list`: skip parcel when oldest year < cutoff.
- [ ] king_wa_tax_delinquent.py: same cap (CONFIRM — user named only Snohomish).
- [ ] tests for both parse functions.

### Phase 3 — Secondary surfaces (only if needed)
- [ ] Cached-records page reads CountyRecord (bill_year in enrichment_data JSON). Defer unless asked.
- [ ] Segments / dashboard "today" — confirm if tax rows there need the cap.

## Codex reconciliation (gpt-5.5, consult 2026-06-16)
- #2 cap rule: BOTH Claude+Codex flagged "drop if oldest>18mo" drops currently-delinquent
  parcels w/ old debt. **User CONFIRMED keep this rule (full dissent on record).**
- #1 min_months>18 → empty set: inherent (cap keeps <=17.5mo, min_months>=18 wants older →
  disjoint). Backend stays HONEST (empty). FOLLOW-UP: frontend should drop min_months options
  >18 for tax jobs. Not blocking.
- #3 ADOPTED: scraper + read-layer = defense-in-depth, predicate from ONE helper.
- #4 ADOPTED: chokepoint — `tax_cap_condition(today)` (ORM) + `tax_cap_sql(alias)` (raw SQL fragment).
- #5 ADOPTED: freeze `today` per request, use UTC (matches existing build_tax_conditions),
  document calendar-YEAR-granularity approximation (Jan-2025 bill ~17.5mo, flips fast).

## Shared predicate (DONE, foundational — tax_filters.py)
- `DEFAULT_TAX_CAP_MONTHS = 18`
- `tax_cap_min_year(today)` → reuses bill_year_bounds_for_months(None,18,today)[1]
- `tax_cap_condition(today)` → `or_(bill_year IS NULL, bill_year >= min_year)` — SELF-SCOPING
- `tax_cap_sql(alias)` + bind `:tax_cap_min_year` — raw-SQL twin

## Orchestration (agents, disjoint files)
- Agent A: jobs.py results list/count/CSV download (+ tests)
- Agent B: segments.py (4 raw SQL) + batch_export.py (_COMBINED_SQL) (+ tests)
- Agent C: dialer_outbox.py + scheduler_helpers/dialer.py (+ tests)
- Agent D: snohomish + king scrapers parse cap (+ tests)  [Phase 2 future-clean]
- Then: central pytest/ruff + diag re-run (4269→~2253) + Codex diff review (NO-GO on Crit/High)

## Review — DONE 2026-06-16
**Shipped (uncommitted, branch test/ui-tax-date-column):** 8 source + 5 test files.
- tax_filters.py: `DEFAULT_TAX_CAP_MONTHS=18`, `tax_cap_min_year`, `tax_cap_condition` (ORM,
  self-scoping), `tax_cap_sql`+`TAX_CAP_BIND` (raw-SQL twin).
- Read-layer cap (hide existing, ALL counties): jobs.py results list+count+CSV; segments.py
  ×4 raw SQL (intersection/union/dated/excluded-no-date) + binds; batch_export _COMBINED_SQL;
  dialer_outbox (+ skipped-status guard so capped leads aren't resurrected by replay);
  scheduler_helpers/dialer push sweep (count+fetch).
- Ingestion cap (future-clean): snohomish + king parse drop parcel when oldest year < cutoff
  (opt-in `cap_min_year` param, None=no cap; capped_out in stats + completion log).
**Verified:** ruff clean (8 files); all modules import; 21 parser tests pass; segments no-DB
guard tests pass (live-DB tests need CI — no local test DB). **Prod read-only proof:**
snohomish 4269→2253 visible (2016 hidden), king 165→165, chelan/clark/skagit unaffected.
**Codex review (gpt-5.5): GATE PASS** — 0 P1, 2 P2 (year-boundary today-drift) BOTH FIXED
(segments `_count_excluded_no_date` now takes frozen `today`; snohomish single `_now`).
**Why "all counties" is satisfied:** only King+Snohomish populate delinquent_bill_year; the
self-scoping predicate caps them everywhere + auto-covers any future bulk-tax county; other
counties' tax records have NULL bill_year (date-windowed at scrape → already <=18mo) and pass.
**FOLLOW-UP (not blocking):** frontend should drop min_months filter options >18 for tax jobs
(now always-empty under the cap). Cached-records page (/scrapers/{id}/records) reads
CountyRecord (no bill_year col; bill_year in enrichment_data JSON) — NOT capped; defer unless asked.
**NOT committed/deployed** — awaiting user go-ahead.
