# Fix: "Fields to collect" checkboxes are cosmetic → make them functional at the output boundary

## Problem (verified in code, not assumed)
The new-scraper wizard's "Fields to collect" checkboxes are 100% cosmetic. The selection is
validated (`FieldsConfig`, `src/api/schemas.py:354`) and persisted (`scraper_configs.fields`,
`src/db/models.py:228`), but **no worker or export code ever reads `config.fields`**. Every
field is always scraped, stored, and written to the delivered CSV.

Verified load-bearing map: `parcel_id`, `party_name`, `property_address`, `mailing_address`,
`date_recorded` are consumed by enrichment / property_key / dedup / billing / skip-trace and
**cannot** be dropped at scrape/storage time. Only `heirs` (pure display) and
`legal_description` (only feeds the within-job idempotency fingerprint) are safe to suppress
at output.

Skip-trace mapping was also checked: frontend "Skip trace" → top-level `skip_trace_enabled`
(the field the worker honors). **No bug there** — verified, no change needed.

## Decisions (user + Codex consult, 2026-06-22)
- **Output-boundary filtering**, never gate scrape/enrichment on `config.fields`. (Codex + Claude agree.)
- **Force identity fields ON** — only `mailing_address`, `heirs`, `legal_description` are hideable.
  The 4 identity fields (`party_name`, `parcel_id`, `property_address`, `date_recorded`) always export.
- **Blank values, keep headers** — do NOT drop columns (stable schema for dialer/webhook consumers).
- **Empty/None/list `fields` → hide nothing** (legacy/default = all visible). Only an explicit
  `False` on a hideable field hides it.
- One **shared projection function** used by every export path (no per-path rules).

Codex session: `019ef0c7-1e11-7463-abca-e74018bfc2f6` (saved for follow-ups).

## Phase 1 — single-job export (the 95% path)  [<=5 files]
- [ ] `src/utils/lead_export.py`: add `HIDEABLE_OUTPUT_FIELDS = {"mailing_address","heirs","legal_description"}`,
      `resolve_hidden_output_fields(config_fields) -> set[str]`, and `_apply_visibility(row, hidden)`.
      Thread `hidden_fields: set[str] | None = None` into `write_lead_csv`.
- [ ] `src/utils/data_exporter.py`: thread `hidden_fields` through `to_csv`, `to_excel`
      (`_canonical_dataframe`), `to_json`, and `export()`.
- [ ] `src/workers/tasks.py:780,995`: pass `hidden_fields=resolve_hidden_output_fields(config.fields)`
      (config already in scope).
- [ ] `src/api/routes/jobs.py:944`: load the job's `ScraperConfig.fields` (scoped to `user.id`),
      pass `hidden_fields` to `write_lead_csv`.
- [ ] Verify: `python -m py_compile`, `ruff`, targeted pytest. Codex reviews the diff.

## Phase 2 — batch combined export  [<=2 files]
- [ ] `src/utils/lead_export.py`: thread `hidden_fields` into `write_lead_csv_with_overlap` /
      `build_overlap_export_row`.
- [ ] `src/workers/batch_export.py:267`: resolve the batch's shared `fields` and pass through.
- [ ] Verify + Codex review.

## Follow-up (frontend repo `bridgeleads-web`, separate — note only)
- [ ] Lock/disable the 4 identity checkboxes (they're now intentionally non-hideable).
- [ ] Relabel "Fields to collect" → "Fields to include in export" (Codex: UI lies about scope;
      backend semantics are output-visibility, not collection).

## Review
(filled at end)
