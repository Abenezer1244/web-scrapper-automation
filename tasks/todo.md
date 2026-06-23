# Doc-Type Visibility (SHOW) + Selection (SELECT) — Build Plan

Branch: `feat/doc-type-visibility` (worktree `.claude/worktrees/doc-type-visibility`, off origin/main @ 19f1ebc which includes #109).
Origin: a customer asked "which pre-foreclosure type do you scrape per county, and why do most counties / all probate show no document types in the wizard?"

Customer decisions (confirmed):
- Deliver **Both, phased**: SHOW first (read-only, all counties + all record types incl. probate), then SELECT (control) layered county-by-county.
- Record-type scope: all live record types (probate, pre_foreclosure, divorce, tax_delinquent, code_violation). NOTE: `eviction` is NOT live (excluded in `src/config/constants.py:156`).

Architecture (reconciled with Codex — its redesign won over my original central-catalog idea):
- SHOW is driven by **scraper-owned pure descriptors**, NOT a duplicated central catalog (catalog would drift from real scrape behavior).
- Each scraper/template exposes `collection_scope(record_type) -> CollectionScope` derived from its OWN existing constants. API resolves the scraper via the registry (as today) and calls the descriptor.
- Honesty flags: items carry `exact: true/false` (portal dropdown/checkbox label = exact; keyword-derived = approximate). `kind: "document_type" | "dataset"` (tax/code_violation pull from Socrata/ArcGIS datasets, not recorder doc types).
- SHOW and SELECT kept separate. Do NOT reuse the existing `doc_types` SELECT field/param for SHOW.

---

## DONE

- [x] Map what each pre_foreclosure scraper actually collects (King=NTS only; Pierce=NOD default + others). Matches customer's ranking.
- [x] Root-cause why most counties / all probate show no selector: `pre_foreclosure_doc_types` API field is gated to record_type==pre_foreclosure AND county supported_for_selection=True (king+pierce only). Probate has no doc-type machinery at all.
- [x] **Clark mismatch investigated + fixed** (commits `82eb674` Codex PASS, `143ddb9` comment correction).
  - Live-verified all 5 Clark pre_foreclosure checkbox IDs against the portal modal (2026-06-22): 167=NOTICE OF TRUSTEE SALE, 129=LIS PENDENS, 166=NOTICE OF DEFAULT, 157=NOTICE OF FORECLOSURE, 93=FORECLOSURE.
  - Root cause of 6-labels-vs-5-IDs: ID **257=TRUSTEES SALE** was missing from the checkbox set. Added it → lists align 6:6.
  - **Corrected a false codebase assumption:** the modal checkboxes ARE the primary server-side gate (portal filters by selected doc-type codes — verified: "DEF"→0 records; OLD 5-set and NEW 6-set both returned 137 records 01/01-06/22). The old comment claiming "portal returns everything regardless" was wrong; client-side keyword filter is defense-in-depth, not the sole gate.
  - **257=TRSL is an empty category** (0 records in trailing 6 months; real trustee-sale leads coded NTS/167). So the 257 add is correct completeness but recovers no leads today → no production lead loss ever existed.

## OPEN FINDING — RESOLVED

- [x] **Clark `DEFAULT` (modal ID 66, code DEF):** sample-scraped live → **0 records** in 3.5 months. Dead/unused category. Decision: do NOT add. (Real notice-of-default leads come via 166=NOTICE OF DEFAULT, already collected.)
- [ ] Latent (note, not this PR): since Clark filters server-side by checkbox codes, the *completeness* of `_DOC_TYPE_CHECKBOX_VALUES` is load-bearing for every record type. probate/divorce/tax code lists were NOT re-verified — worth a future audit pass.

## PHASE A — SHOW (read-only transparency)  [each sub-phase <=5 files, verify between]

- [ ] A1. Define `CollectionScope` shape + a base hook on the scraper interface (`base_scraper.py`) returning a safe default so unconverted scrapers degrade to null.
- [ ] A2. Implement `collection_scope()` for the recorder TEMPLATES (eagleweb, acclaimweb, ava_fidlar, idocmarket, landmarkweb, laserfiche_weblink, skagit_recording, tyler_selfservice) deriving labels from each `_DOC_TYPE_MAP`; mark keyword-derived items `exact=false`.
- [ ] A3. Implement for the bespoke recorder scrapers (king=search_text/exact, pierce=checkbox/exact-ish, clark=verified labels exact, whatcom, snohomish).
- [ ] A4. Implement for dataset scrapers (tax_delinquent, code_violation) -> `kind:"dataset"`, empty items, honest `note`.
- [ ] A5. API: add nullable `collection_scope_by_record_type` to the connector response, populated for ALL record types. Keep `pre_foreclosure_doc_types` (SELECT) untouched. Regenerate + commit `schema/openapi.json` backend-first.
- [ ] A6. Coverage test: every active connector x record_type returns a scope (no silent gaps). Plus a test that SHOW never returns canonical SELECT tokens.
- [ ] A7. Codex review the full Phase A diff; reconcile; ruff + pytest (unit-only under synthetic env — conftest wipes tables).
- [ ] A8. Frontend (separate repo bridgeleads-web): wizard renders read-only "Document types collected" list for all counties + record types. (Separate session/PR.)

## PHASE B — SELECT (control), later
- [ ] Generalize the `doc_types` selection param + validation beyond pre_foreclosure, gated per (county, record_type) `supported_for_selection`.
- [ ] Per-county live-portal verification before flipping each `supported_for_selection=True`.

## Review
(to be filled in after Phase A)
