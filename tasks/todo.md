# Lead-Quality Field Build (record-type gap analysis → implementation)

**Source:** `docs/research/record-type-fields/00-GAP-ANALYSIS.md` (6-type investor-demand
research vs BridgeLeads delivery). **Architecture: Codex-consulted 2026-06-12** (verdict below).
Build order = Tier 0 (cheap wins, data we already hold) first, 1 by 1, Codex-gated each.

## Codex architecture verdict (locked)
1. **Absentee/out-of-state** → STORED nullable columns + Python normalizer + chunked backfill.
   NOT generated columns (address parsing too messy for an IMMUTABLE SQL expr). Add
   `owner_state`/`property_state` for explainability + filtering.
2. **Enrichment passthrough** (assessed_value, code-violation type/status/desc, tax billed/paid/
   account_status, instrument_number) → CSV columns YES, DB columns NO. Read from `enrichment_data`
   at export. Cap ~15-20 type-specific cols before per-type presets.
3. **Derived** (months_delinquent, wa_foreclosure_eligible, freshness_days, contactability_score)
   → DERIVE everywhere, never store. freshness_days decays daily — must be query-time.
4. **stacked_distress_count** → opt-in projection on list/detail/export paths, NOT base
   get_results. Materialized summary table only if it becomes a primary filter/sort.
5. Generated columns = wrong for address intelligence (high IMMUTABLE risk).
6. Sequence: C first (zero migration), then D/B derived, then A (first migration), defer E.

## Phases (≤5 files each, Codex consult-before + review-after, verify tsc-equiv/lint/tests)

- [x] **Phase 1 — Export captured-but-dropped enrichment fields** DONE (`d67b567`+`01db03f`,
      Codex review PASS, alias fix adopted). 9 CSV cols from enrichment_data. Note: ResultRow
      already exposes enrichment_data raw → UI change is frontend-only; kept Phase 1 to the CSV.
- [x] **Phase 2 — Derived signal fields** DONE (`92b4ce3`+Codex-fix commit). `lead_signals.py`
      (months_delinquent, wa_foreclosure_eligible, freshness_days, contactability_score 0-6).
      Codex review: 3 fixes adopted (E.164 phone dedup, exact tax-filter months parity, single
      today for Excel). Wired CSV + ResultRow.
- [~] **Phase 3 — Absentee / out-of-state owner** (MIGRATION; Codex design-consulted). Sub-phases:
      - [x] **3a** code DONE: `address_intel.py` (compute_owner_flags — component compare, unit-only
            ≠ absentee, suffix/dir canonical, tri-state NULL); migration 057 (4 nullable cols, NO
            indexes); Result model cols; **single end-of-job recompute choke point** at the
            post-enrichment refetch (tasks.py:898 — `daily_scrape` writes CountyRecord not results,
            so run_scrape_job is the sole results writer; reuse-for-dups runs inside enrichment
            before the refetch) + best-effort populate-at-insert. 16 tests. **Pending: unit-suite
            green → commit → Codex review.**
      - [x] **3b** code DONE: `scripts/backfill_owner_flags.py` (chunked 1-5k, resumable on
            all-4-NULL window, system role FOR ALL, dry-run default). Pending Codex review.
      - [ ] **3c**: filter predicates (mirror tax_filters, IS TRUE not truthy) + CSV cols + ResultRow
            fields + out-of-band CONCURRENT partial indexes. Deploy: merge 057 → run backfill.
- [ ] **Phase 4 — stacked_distress_count** (opt-in projection). Grouped subquery on
      property_list_membership keyed by (user_id, property_key); join only on detail/export, not
      base get_results. Surface in export + ResultRow when requested.
- [ ] **Phase 5 — Death-cert heirs → skip trace** (workflow). Ensure `heirs` (grantee) feeds the
      Tracerfy enqueue the way party_name does, for death_certificate leads; heir-out-of-state
      reuses Phase 3 normalizer.

## Tier 1 (after Tier 0 ships; bigger builds, agent-orchestrated, separate planning)
- NTS document-image parse → pre-foreclosure auction date / default amount / trustee / TS#.
- Probate superior-court docket scrape → PR + attorney + case# + filing date.
- Equity estimate (buy-vs-build AVM/lien feed) — needs a product decision.

## Review
- _Phase 1 in progress._
