# Task: Standardize tax-delinquent "amount owed" + fix King scraper bug

## Context / why
- User goal: make the "amount owed" number consistent across all tax-delinquent counties, the "Snohomish way" (the fuller balance), and have it be what a wholesaler actually wants (true distress signal).
- INVESTIGATION (this session, verified live against county data):
  - **King's `dsv3-ct3e` scraper is structurally broken.** It filters `receivable_type='D'` believing D = "Delinquent", but the whole dataset is already delinquent and `D` = **D**rainage district assessment (one minor charge type). Result: catches **178 of 28,609** delinquent parcels (~0.6%) and reports a tiny drainage line (~$91) instead of the real balance ($15k+).
  - King's authoritative code dictionary (dataset `dyps-vajd`): all `receivable_type` codes are PRINCIPAL charges (R=Real Property Levy, U/X=Surface Water, V=Conservation, N=Noxious Weed, D=Drainage, E=Fire, F=Forest Patrol, I=Irrigation, A=Abatement). **No penalty/interest code exists** — King computes those at payment time only.
  - Snohomish col 16 "amount owed" is (strong evidence, official PDF unread) **also principal-only**.
  - => "Full payoff incl penalty/interest" is NOT available from either county's bulk data. Achievable consistent standard = **total delinquent principal (tax + assessments) summed per parcel across delinquent years** — which Snohomish already does and King does not.
- Decisions locked with user: (1) fix bug + standardize together; (2) target the fullest number the data supports, labeled honestly.
- OPEN (pending LLM Council verdict): include ALL charge types (A) vs only R real-property levy (B); how to handle A=Abatement; column label.

## Plan (phased — max 5 files/phase, verify between phases)

### Phase 0 — decisions (IN PROGRESS)
- [x] Confirm King data structure + bug magnitude (live API)
- [x] Confirm King receivable_type code meanings (research agent)
- [x] Confirm Snohomish col 16 semantics (research agent)
- [ ] LLM Council verdict on A-vs-B + abatement + label (running)
- [ ] Codex pressure-test of the chosen design (before code)

### Phase 1 — King scraper rewrite (1 file) — DONE
- [x] `src/scrapers/king_wa_tax_delinquent.py`: dropped `receivable_type='D'` filter; pure `aggregate_delinquent_rows()` sums (billed-paid) across included charge types + all delinquent years per parcel; `bill_year`=min; conservative current-year exclusion (matches Snoho); A=Abatement excluded+alert; unknown codes fail-closed+alert; 12-digit real-property gate; floor at parcel total; per-type/per-year breakdown stored; canary on zero-parcels.
- [x] Full-pagination-before-emit generator (fixes old dedup-first-row hazard).

### Phase 2 — tests (1 file) — DONE
- [x] `tests/test_king_tax_delinquent.py` (7 tests): per-parcel sum across types+years, same-parcel-multi-account grouping, bill_year=min, abatement+unknown exclusion+alerts, malformed quarantine, current-year/out-of-range exclusion, net-zero drop, Decimal precision. **ruff clean, 39/39 pass.**

### Phase 2.5 — LIVE validation (council's #1 priority) — DONE
- [x] Old D-only vs new (years<=2024): **41 -> 3,892 parcels (~95x).**
- [x] Real parcel 7534800005: NEW=$2,982,616.57 (2024+2025, 2026 excluded) vs OLD=$0.00 (no D line -> was invisible). Math + current-year rule + bill_year=min all verified on live data.

### Phase 3 — labeling + doc — DONE
- [x] Agents confirmed: CSV header is the literal key `delinquent_amount` (KEEP — dialer/test contract); display labels live in frontend.
- [x] Frontend honest labels (bridgeleads-web): ResultsTable.tsx header "Amount Owed"→"Tax Balance Owed", "Tax Year"→"Oldest Tax Year"; ResultsToolbar.tsx help text now states "principal only — excludes penalties & interest". **frontend tsc clean.**
- [x] Qualification doc §3: added RESOLVED STANDARD (sum all charges across years per parcel; King fixed; penalty/interest deferred).

### Phase 3.5 — Codex implementation gate — DONE
- [x] Codex final review: **P1 none.** Validated cent math, floor-at-parcel-total, generator consumption, current-year math, fixture math. 1 P2 (silent partial-pagination truncation) → FIXED: `_iter_api_rows` now RAISES on mid-pagination fetch error (no silent truncation). Re-verified ruff + 7/7 tests.
- [x] Dedup/billing safety verified (agent): delinquent_amount NOT in dedup_hash/property_key/billing; re-scrape overwrites via COALESCE(new,old).

### Phase 4 — existing-data remediation — RESOLVED (backfill DROPPED)
- [x] Sized it (read-only): 59,380 rows / 3,400 parcels / 14 jobs / **2 tenants**; cache=0.
- [x] Built backfill script (Codex-gated, plan→guard→apply). **DRY-RUN guard ABORTED correctly:** only 62/3,400 old parcels still match today's delinquent set — old data is ~95% current-year (2026) point-in-time snapshots; overwrite would NULL 98%.
- [x] DECISION (user): **drop the overwrite backfill** (wrong tool for point-in-time historical data) → **fix-forward**; re-run King for the 2 tenants for fresh correct lists. Scratch scripts removed.

### Phase 5 — current-year inclusion (surfaced by Phase 4) — DONE (committed to PR #52)
- [x] DECISION (user): King INCLUDES current-year (delinquent-only source; WA first-half-miss accelerates full year per RCW 84.56.020; ~99% of King is current-year). Snohomish still excludes (lists all parcels).
- [x] Scraper effective_end cap current_year-1 → current_year; doc §2 updated. ruff + 7/7 tests pass. Pushed (commit 2d92836).
- [x] CONFIRMED with user: target = "previous ~1–1.5 years" delinquency → served by scrape date range + `max_months` filter (bill_year=oldest). OPEN: set a default ~18-month window? (awaiting user)

### Phase 6 — default ~18-month window (user: "all counties tax deli") — DONE + MERGED
- [x] Backend `_resolve_date_range`: record_type=tax_delinquent → 548-day default (all counties; only the default path; explicit modes win). Caller passes record_type. 5 tests. Codex: no P1.
- [x] Frontend wizard: tax_delinquent defaults to 18-month custom range (UI matches backend). tsc clean.
- [x] Verified King/Snoho connectors have max_date_range_days=None → 18mo NOT clamped (Chelan=30 but down).
- [x] **MERGED: PR #52 (backend→main) + #24 (frontend→master), squash, branches deleted. Auto-deploying.**

### Phase 7 — re-run for 2 tenants — DONE ✅
- [x] Deploy confirmed live (re-run jobs resolved to 12/14/2024→06/15/2026 = 548d = 18mo default proves new code).
- [x] Fired focused re-run (mint create_secure_token + POST prod /jobs) for 1 King config/tenant. Both jobs scraped **28,496 parcels** (vs old 178) — fix VALIDATED end-to-end in prod. Now in skip-trace enrichment → done.
- [x] BUILD_JOURNAL + memory written. Scratch scripts removed.

## ✅ COMPLETE — all phases done, merged, deployed, prod-validated.
Residual (👤 owner, optional): re-run other King/tax scrapers from dashboard (now 18mo default). Uncommitted local docs: BUILD_JOURNAL.md + this todo (offer to commit).

## Open follow-ups (user/ops)
- 👤 Owner can re-run other specific King/tax scrapers from the dashboard (now defaults to 18mo) — focused script only does 2 representative configs.

## Verification gates
- [ ] `ruff check` + `pytest tests/test_king_tax_delinquent.py tests/test_tax_fields_extract.py tests/test_tax_filters.py`
- [ ] Live King scrape sanity (railway run worker): parcel count ~thousands not ~178; amounts realistic.
- [ ] Codex review of the diff (gate). Any Critical/High = NO-GO.

## Review
_(to be filled at end)_
