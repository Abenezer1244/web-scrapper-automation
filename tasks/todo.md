# Task: Unify lead CSV export (one dialer-ready format everywhere) + relocate DNC disclaimer

Codex-consulted plan (GATE pending per phase). Goal: download AND emailed/R2 CSV produce the SAME
dialer-ready file, so no "tell users which to use". Kill the two-builder drift.

## Phase 1 — pure canonical builder + golden tests
- [ ] `src/utils/lead_export.py`: `LEAD_CSV_COLUMNS`, `_get(record,name)` (dict.get OR getattr),
      `build_lead_export_row(record)` (reuses lead_formatting: normalize phones, split name/address,
      sanitize text, plain numerics; secondary contacts from BOTH phones/emails arrays AND flattened
      phone_2/email_2 keys), `write_lead_csv(records, filelike)`.
- [ ] `tests/test_lead_export.py`: golden tests for dict input AND ORM-like object input.

## Phase 2 — switch download
- [ ] `jobs.py download_export` uses write_lead_csv; prove column/format parity with current download.

## Phase 3 — switch DataExporter + remove footer
- [ ] `DataExporter.to_csv` + `to_excel` use canonical rows (Excel = same table). REMOVE the `#` DNC footer.
- [ ] JSON left as-is (separate, versioned — do NOT canonicalize silently).

## Phase 4 — relocate disclaimer (SAME release as footer removal)
- [ ] DNC/TCPA disclaimer → delivery email body (delivery.py) + download UI (frontend). Keep prominent.

## Phase 5 — polish
- [ ] Fix the lying download docstring ("Stream from R2" → builds live DB CSV).
- [ ] Codex review gate (NO-GO on Crit/High) → ship.

## Notes
- Honest caveat (Codex): unifying aligns FORMAT, not data freshness — emailed snapshot can be staler than
  a live download. Separate axis; not solved here.
- Compliance (Codex/FTC): notice placement != compliance. Real obligation = DNC scrub ≤31d + records
  (dialer/process). Footer removal is operationally correct.
