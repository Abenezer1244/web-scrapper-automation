# Lean per-record-type export columns

## Problem
The lead CSV is ONE fixed 49-column schema (`LEAD_CSV_COLUMNS`) emitted for every
record type. A Pierce probate export ships ~16 structurally-blank columns that
belong to other record types (tax / code-violation / pre-foreclosure-NTS). Same
in reverse for every type.

## Decision (user)
**Lean by default** for single-record-type exports: drop the columns a record type
can NEVER populate. Combined/batch + segment exports stay the FULL superset.

## Design (reconciled with Codex — consult session 019f23f0)
- Declarative `record_type -> columns` map. NOT data-driven ("all empty in this
  export" != irrelevant — failed enrichment / filtered rows / async-not-landed).
- Unknown/new record_type -> FULL (never silently drop data).
- ONE seam `resolve_lead_export_columns(record_type)`; every single-type file path
  routes through it. No second CSV builder.
- Keep row building full-width; restrict FIELDNAMES only (csv extrasaction="ignore",
  DataFrame column projection). The existing hide-fields mechanism (blank-but-keep-
  header) is SEPARATE and composes: lean picks the column SET, hide blanks values.
- Excel + JSON follow CSV (no format drift). Batch-combined stays superset.
- Do NOT touch webhook/dialer push JSON (separate contract).

## Per-type column map (code-grounded)
BASE (32, all types): date_recorded, party_name, heirs, parcel_id,
property_address, mailing_address, legal_description, doc_type, phone, phone_type,
email, phone_2, phone_3, email_2, email_3, first_name, last_name,
property_street/city/state/zip, assessed_value, instrument_number, freshness_days,
contactability_score, absentee_owner, out_of_state_owner, owner_state,
mailing_street/city/state/zip.
  NOTE heirs is BASE (kept for every type): it is a shared secondary-party column
  populated as heirs (probate), other spouse (divorce), OR the opposite party on a
  pre_foreclosure filing (orient_pre_foreclosure_party -> record.heirs). Dropping
  it lost pre_foreclosure data — Codex [P1], fixed.
- probate: +lead_subtype
- death_certificate: (base only)
- divorce: (base only)
- eviction: (base only)
- tax_delinquent: +delinquent_amount, delinquent_bill_year, tax_billed_amount,
  tax_paid_amount, tax_account_status, months_delinquent, wa_foreclosure_eligible
- code_violation: +code_violation_type/status/description/last_inspection
- pre_foreclosure: +auction_date, days_to_auction, default_amount, trustee, ts_number

## Plan
### Phase 1 — core (lead_export.py) + unit tests
- [ ] Add `_TYPE_EXTRA_COLUMNS`, `LEAN_BASE_COLUMNS`, `resolve_lead_export_columns()`
- [ ] `write_lead_csv(..., columns=None)` -> DictWriter(extrasaction="ignore")
- [ ] tests: map correctness, order preserved, unknown->full, subset of superset

### Phase 2 — wire single-type paths
- [ ] data_exporter: `columns=` on to_csv/to_excel/to_json/export + _canonical_dataframe
- [ ] jobs.py download_export: resolve from config.record_type
- [ ] tasks.py:962 + :1236 scheduled R2 export: resolve from config.record_type
- [ ] batch_export + segments: leave unchanged (superset)

### Phase 3 — verify + review
- [ ] pytest (export tests) green; import smoke
- [ ] Codex review of the diff; fix Critical/High
- [ ] Security master-review (CSV injection unaffected — same builder)

## Review
(to fill in)
