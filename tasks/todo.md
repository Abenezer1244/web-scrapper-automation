# Chelan County — Fix scraping + add all 6 record types

**Date:** 2026-04-11 (session 4)
**Goal:** Make Chelan's AcclaimWeb portal fully working and wire up all viable record types.

## Known issues from probe

1. `#RecordDate` single-date field ignores `press_sequentially` — DatePicker widget swallows keystrokes, value stays stale. Fix: force-set via JS eval + trigger change event as fallback.
2. AcclaimWebScraper missing `record_type: str | None = None` kwarg (Phase A only fixed EagleWeb + TylerSelfService). Chelan gets mixed types until this is added.
3. Results table is plain HTML (not Kendo grid) — needs the header-aware fallback path (commit `67b66cd`). The fallback exists but may not be triggering for Chelan's specific table structure. Need to debug.

## Phase 1 — Fix base scraping

- [ ] 1. Add JS fallback to `_fill_dates` in `acclaimweb.py`: after typing, verify value via `get_attribute("value")`; if wrong, force-set via `el.value = X; el.dispatchEvent(new Event('change'))`
- [ ] 2. Add `record_type: str | None = None` kwarg to `AcclaimWebScraper.__init__` (same pattern as EagleWeb Phase A)
- [ ] 3. Use `self.active_record_type` in the extraction filter (if AcclaimWeb has one — check)
- [ ] 4. Probe Chelan probate (14-day window) — target: > 0 records with party data

## Phase 2 — Test each record type (1 by 1)

For each type: probe 30-day, check for > 0 records, document what doc_type labels appear.

- [ ] 1. **probate** — Death Certificate, Personal Representative Deed, Will, Transfer on Death, Affidavit of Heirship
- [ ] 2. **pre_foreclosure** — Notice of Trustee Sale, Lis Pendens, Notice of Default
- [ ] 3. **tax_delinquent** — Tax Lien, Certificate of Delinquency (may be blocked by RCW 42.56.070(8))
- [ ] 4. **divorce** — Decree of Dissolution (county recorder only, court records blocked)
- [ ] 5. **code_violation** — unlikely on recorder portal (municipal source), document if absent
- [ ] 6. **eviction** — court records, blocked at source (LINX/Odyssey), document

## Phase 3 — Wire up + promote

- [ ] 1. Update Chelan's `record_types` in DB to include all verified types
- [ ] 2. Promote Chelan to `health_status=healthy` if probate + at least 1 other type works
- [ ] 3. Commit + push (backend auto-deploys to Railway, frontend already marks "In progress" counties dynamically)
- [ ] 4. Verify live API shows Chelan as healthy with updated types

## Review
(Fill in after execution)
