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

- [ ] **Phase 1 — Export captured-but-dropped enrichment fields** (no migration). `lead_export.py`
      +8 CSV cols read from `enrichment_data` (assessed_value, instrument_number,
      code_violation_type/status/description/last_inspection, tax_billed_amount/paid_amount/
      account_status); `schemas.py` ResultRow typed top-level fields; tests. Keys verified:
      `enrichment_data["assessed_value"|"instrument_number"|"record_type"|"description"|"status"|
      "last_inspection"|"billed_amount"|"paid_amount"|"account_status"]`.
- [ ] **Phase 2 — Derived display/sort fields** (no migration). New pure-function module
      `src/utils/lead_signals.py`: months_delinquent + wa_foreclosure_eligible (from bill_year),
      freshness_days (from date_recorded_parsed/date_recorded), contactability_score (phones/emails).
      Wire into ResultRow serialization + CSV export. Tests.
- [ ] **Phase 3 — Absentee / out-of-state owner** (MIGRATION). Normalizer in `lead_signals.py` (or
      new `address_intel.py`): normalize_address + extract_state. Stored cols `absentee_owner`,
      `out_of_state_owner`, `owner_state`, `property_state` (nullable). Populate at insert
      (tasks.py), chunked backfill script, partial indexes for the bool filters, export + ResultRow
      + filter support (mirror tax_filters). Migration 057.
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
