# 08. Where personal data leaves the backend outside the product (Phase 6 P4e)

Date: 2026-11-06. Scope: `scripts/` (Python and SQL), admin routes, ops alerts, log lines and
error tracking in the backend repo. Customer-facing product paths are covered by the read
inventory (`07-RESULTS-READ-INVENTORY.md`) and the send-path work (P4a to P4d). Method: code
reading only; a sample of findings was re-checked by hand (`verify_tracerfy_provenance.py`,
`verify_entitlement_deploy.py`, `.gitignore`).

"Personal data" here means data about lead subjects (owner, party, heir and grantor names,
property and mailing addresses, parcel ids, phones, emails) or about customers (email, name).

## Summary

| Area | Finding | Severity |
|---|---|---|
| Product code (`src/`) | Ops alerts, Sentry and log lines carry ids and counts. No log line writes a name, address or contact detail. | Low |
| Admin routes | Three `require_admin` routes, none returns personal data. No debug endpoints. | Low |
| Error tracking | Allowlisted: exception type and frame positions; messages redacted, request data, user, locals and breadcrumbs dropped. | None |
| Logs | Customer email at the call site (masked by the global filter to `j***@domain`); full parcel ids in enrichment logs. | Medium |
| Scripts | About 30 operator scripts print or write subject or customer data, unfiltered. | High |
| Privacy flag | No script applies `results.privacy_suppressed_at`. | High (before activation) |

## A. Scripts (operator-run, usually `railway run`, output to the operator's terminal or a local file)

**Bulk or contact-level (High):**

| Script | Emits | Destination |
|---|---|---|
| `verify_tracerfy_provenance.py:85-90` | party name, property address, decrypted phone, email, phones, emails | stdout |
| `purge_test_batch_configs.py:177` (`--backup`) | `SELECT *` of results and related rows | local JSON file |
| `verify_entitlement_deploy.py:175` | every user's email, plan and usage | local JSON baseline |
| `diag_test7_rows.py:46` | full rows for one job (names, heirs, parcel, addresses, enrichment) | stdout JSON |
| `diag_test11_dq.py:62` | every scraped row of a live scrape | `test11_dq.json` (now ignored) |
| `diag_test7_source_capture.py:36-39` | raw and parsed recorder rows | JSON in an output dir |
| `backfill_preforeclosure_party_names.py:146,150` | old and new party and heirs | stdout + `scripts/audit_out/` CSV |
| `backfill_king_code_violation_owner.py:235,394` | addresses, old and new party | `king_*` JSONL (ignored) |
| `repair_stale_absentee_owner.py`, `backfill_assumed_mailing.py`, `backfill_pierce_statewide_mailing.py`, `king_rpacct_mailing.py`, `king_taxbill_mailing_check.py`, `repair_king_property_situs.py`, `backfill_property_situs_parts.py`, `repair_placeholder_absentee.py`, `backfill_king_code_violation_mailing.py`, `release_stranded_dedup_claims.py` | parcel, property and mailing addresses, some party names | root-level JSONL (now ignored) + stdout |

**Cross-tenant reads to stdout (High/Medium):** `diag_king_bad_parcel_rows.py`,
`diag_probate_party_scan.py`, `diag_preforeclosure_party_names.py`, `diag_xcheck_followup.py`,
`diag_xcheck_king_and_canary.py`, `diag_nts_pointer_integrity.py`, `diag_nts_recent_matches.py`,
`diag_nts_null_downgrade.py`, `diag_king_ts_repair_plan.py`, `repair_nts_ts_number.py`,
`diag_nts_stored_vs_source.py`, `diag_skip_trace_match_audit.py`, `diag_match_key_collisions.py`,
`rerun_pierce_address_recovery.py`, `diag_king_tax_owner_results.py`.

**Customer emails to stdout (Medium):** `diag_scraper_name_collisions.py`,
`audit_entitlement_violations.py` (decrypted), `sprint4_three_counties_prod.py`; and the
operator-supplied email echoed by `generate_break_glass.py` (lands in Railway logs),
`onboard_customer.py`, `stripe_single_customer_promo.py`.

**Live public-record scrapes (Medium, not from the database):** `live_verify_*`, `diag_divorce_live.py`,
`live_test_code_violation.py`, `e2e_*`, `test_*_sprint2.py`, `test_*_preforeclosure.py`,
`test_counties_systematic.py`, `test_douglas_prefix.py`, `test_cowlitz_pagination.py`,
`ui_county_audit.py` (screenshots, ignored).

**Ids and counts only (fine):** the PACS/GIS/King-tax repair and requeue scripts,
`backfill_pierce_code_violation_owner.py`, `backfill_mailing_by_county.py`, the PII-encryption
verify/backfill scripts, `data_quality_report.py`, `cv_qualification_report.py`. The `.sql` scripts
are DDL only.

## B. Admin routes

`billing.py:122` (`/activation-funnel`, aggregates through a definer function) and
`scrapers.py:435,993` (connector metadata). None returns personal data.

## C. Logs and alerts

- `ops_alerts.send_ops_alert` goes to email, a Better Stack webhook and `audit_events.detail`.
  All ~40 callers send ids, counts, county names and exception class names; a few send
  truncated exception text (`finalize.py:95` is a DB error with bound parameters hidden). Low.
- `src/utils/logger.py` masks email local parts, labelled phones and Postgres DETAIL text. It
  does not mask names, addresses or parcel ids, and none are logged except parcel ids.
- Customer email at the call site: `delivery.py:272,308,310,359,361,374,415,417`,
  `onboarding_emails.py:331` (masked by the filter; line 359 also logs the client IP). Medium.
- Full parcel ids: `scrapers/enrichment/county_gis.py`, `national.py`, `parcel.py`,
  `clark_pic.py`, `pierce_atip.py`, `pierce_atip_owner.py`, `pierce_legal_repair.py`,
  `pierce_wa_probate.py:572`. Parcel ids are public record keys, but they are also a privacy
  anchor (design section 5). Medium.

## D. Error tracking

`src/utils/error_tracking.py`: allowlist, `send_default_pii=False`, messages redacted, no task
args. Dormant until `SENTRY_DSN` is set (and must be disclosed first). Nothing to fix.

## Fixed in this slice

- `.gitignore` now ignores root-level `*.jsonl` reports and `test11_dq.json`, so a script report
  holding names and addresses cannot be committed by accident. None was tracked.

## Open (owner decisions, before suppression activation in P7)

1. **Scripts must honour suppression.** Once a rule is active, any script that prints or exports
   subject data must exclude `privacy_suppressed_at IS NOT NULL` rows, or be retired. Proposed
   activation checklist item (P7): every High script above either filters with the central
   predicate (`src/api/lead_actionability.py`) or is deleted. Most are one-off diagnostics whose
   job is done; deleting them is the simpler fix.
2. `verify_tracerfy_provenance.py` prints decrypted contacts; it can compare hashes and print
   masked values instead.
3. `purge_test_batch_configs.py --backup` and `verify_entitlement_deploy.py` write bulk personal
   data to an unencrypted local file; default them into an ignored directory and print a warning.
4. Replace plaintext customer emails in `delivery.py` / `onboarding_emails.py` log calls with the
   existing email fingerprint helper; decide whether full parcel ids in enrichment logs are
   acceptable (retention of Railway logs governs the exposure).
