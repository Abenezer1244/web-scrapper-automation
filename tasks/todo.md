# Batch Results collapse + overlaps-export cross-check fixes (2026-07-02)

Branch (BE): `chore/overlaps-xcheck-2026-07-02` (worktree `.claude/worktrees/overlaps-xcheck`, off origin/main)
Branch (FE): `chore/results-batch-collapse-2026-07-02` (worktree `bridgeleads-web/.claude/worktrees/results-batch-collapse`, off origin/master)

## Root cause (user's reported bug)
`GET /jobs` (`list_jobs`, jobs.py) returns every job incl. batch children; the Results page
(`app/(dashboard)/results/page.tsx`) renders one row per done job -> a batch shows N rows
(probate=2, pre_foreclosure=3) instead of ONE combined listing. Batch children have suppressed
delivery (`deliver={}` — the batch owns the combined CSV), so they must NOT appear as standalone
"exports ready to download". The batch should be ONE row with both, linking to the combined view.

## Scope: user chose ALL 4 cross-check findings + the two-listings fix.

### Phase 1 — Two-listings collapse (BE additive + FE) — the reported bug
- [ ] BE `JobResponse.batch_id: str | None` (schemas.py) populated in `list_jobs` from `sc.batch_id`
- [ ] BE `BatchSummaryResponse.combined_record_count: int | None` computed in `_summary` from the
      latest run's `delivery_counts` (mode-aware: overlaps_delivered if overlaps_only else leads_total)
- [ ] Regen `schema/openapi.json` (.venv-schema)
- [ ] FE `listBatches()` in lib/api.ts
- [ ] FE Results page: exclude done-jobs with `batch_id`; add batch rows (from listBatches, run downloadable)
      -> row links to `/batches/[id]`, download via `downloadBatchCsv(id)`; merge + sort by date
- [ ] Tests (BE): jobs list carries batch_id; batch summary carries combined_record_count

### Phase 2 — Combined-export correctness (#1 + #3)
- [ ] BE `_COMBINED_CTES`/`_COMBINED_SQL` (batch_export.py): select the FULL column set
      `build_overlap_export_row` consumes — delinquent_amount, delinquent_bill_year (KILLS the
      fabricated 01/01/{year} tax date), heirs, legal_description, doc_type, phones, emails,
      absentee_owner, out_of_state_owner, owner_state, property_state, auction_date, default_amount,
      enrichment_data (assessed_value/instrument/code_violation/tax/nts)
- [ ] `_combined_pairs`: decrypt phones/emails like segments `_decrypt_pii_rows`; pop enrichment
      `lead_subtype` so the bucket-aggregated `a.lead_subtype` scalar still wins (preserve Codex P2)
- [ ] #3: `_delivery_summary` overlaps_only branch uses explicit singletons_suppressed +
      unmatchable_no_parcel instead of `total - overlaps`
- [ ] Tests: combined CSV populates heirs/tax/phones_2; tax row filed_date is BLANK (no fake date)
- [ ] NOTE: segments.py shares the same fabricated-tax-date bug (out of stated scope) — flag/decide

### Phase 3 — 50k silent truncation (#2)
- [ ] BE finalize/download: page `_COMBINED_SQL` until exhausted (no silent 50k cap) OR surface a
      `truncated` signal + honest email copy. Decide with Codex (memory: "No silent caps").

### Phase 4 — Remove dead `overlaps_first` mode (#4)
- [ ] BE schemas Literal -> drop overlaps_first; models CheckConstraint update; alembic migration to
      alter the CHECK (no existing rows use it); regen openapi
- [ ] FE type regen

### Phase 5 — FE finalize
- [ ] Regen FE api-types from BE openapi; typecheck + lint; QA the Results page collapse

## Codex/verification
- Consult Codex on design BEFORE coding. Codex reviews EACH phase diff. Critical/High = NO-GO.
- No local Postgres -> tests run via PR CI. Backend-first; FE after BE merges + type regen.

## Review

Status: all 4 findings + the two-listings fix IMPLEMENTED, committed on both branches.
Local gates green: ruff (src+tests), FE tsc + eslint, openapi regen (no drift), single
alembic head (080). NOT pushed/PR'd yet (test loop = CI on a PR — no local Postgres).

BE commits (branch chore/overlaps-xcheck-2026-07-02, off origin/main, merged w/ #142):
- Phase 1  774c4b1  JobResponse.batch_id + BatchSummaryResponse.combined_record_count
- Ph2 #1/#3 247407a  combined export selects FULL column set (kills blank cols + fabricated
                     tax date) + phones/emails decrypt + aggregated lead_subtype preserved;
                     _delivery_summary reports singletons vs no-parcel separately
- Ph3 #2   cb52255  _combined_pairs_all pages until exhausted (no silent 50k truncation)
- Ph4 #4   c061b59  remove dead overlaps_first (Literal + CHECK + migration 080)
- merge 5ff04b3 (origin/main #142) + 5672e65 (migration 079->080 collision fix)

FE commits (branch chore/results-batch-collapse-2026-07-02, off origin/master):
- c1724f4  Results page collapses a batch into ONE row (listBatches + exclude batch children)
- 77c9014  drop dead overlaps_first from FE types + stale comments

Codex: DESIGN consult completed — validated all 4 (guardrails folded in: sanitize
enrichment copy, page-until-exhausted, batch-row-only-when-downloadable, migrate-before-
reject). Final DIFF review BLOCKED — Codex quota exhausted mid-session ("try again 5:50 PM").
⏭️ Resume `codex review --base origin/main` (BE) + `--base origin/master` (FE) when reset.

⏭️ Next (user/ops): push both branches, open PRs (BE first), read CI; migrate 080 BEFORE
deploying api+worker; regen FE types from BE main after merge; then FE PR CI/deploy.
Note: partial-status batches show a "Complete" badge (minor) — possible follow-up polish.
