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

- [x] A1. `CollectionScope`/`DocTypeItem` shape (`doc_scope.py`) + `BridgeScraper.collection_scope()` classmethod default None. Commit `ec0abd1`, ruff clean, tests pass.
- [x] A2. `collection_scope()` for the 7 keyword templates (eagleweb, acclaimweb, ava_fidlar, idocmarket, landmarkweb, laserfiche_weblink, tyler_selfservice) via shared `from_keyword_map()`. Presentation layer (Codex-reconciled): broad predicates -> "X-related filings"; cryptic abbrevs -> explicit bucket; divorce -> classifier positives; "signals" framing; coverage test fails on any unmapped keyword. Commit `3e70eec`, ruff clean.
- [ ] A3. Bespoke connectors — judgment-heavy (exact vs approximate labels):
  - king (search_text -> Death Certificate / Notice of Trustee Sale, exact per doc_types.py "verified")
  - pierce (checkbox IDs -> exact: Probate / NOD / Notice of Foreclosure / Lis Pendens / Notice of Trustee Sale / Decree of Dissolution)
  - clark (live-verified labels, exact=True; divorce via classifier)
  - whatcom (Helion keyword filter -> exact=False)
  - snohomish (newspaper, NTS only -> "Notice of Trustee Sale")
  - skagit (server dropdown `_SERVER_DOC_TYPES` exact labels + note about client-side comment refinement)
- [ ] A4. Implement for dataset scrapers (tax_delinquent, code_violation) -> `kind:"dataset"`, empty items, honest `note`.
- [ ] A5. API: add nullable `collection_scope_by_record_type` to the connector response, populated for ALL record types. Keep `pre_foreclosure_doc_types` (SELECT) untouched. Regenerate + commit `schema/openapi.json` backend-first.
- [ ] A6. Coverage test: every active connector x record_type returns a scope (no silent gaps). Plus a test that SHOW never returns canonical SELECT tokens.
- [ ] A7. Codex review the full Phase A diff; reconcile; ruff + pytest (unit-only under synthetic env — conftest wipes tables).
- [ ] A8. Frontend (separate repo bridgeleads-web): wizard renders read-only "Document types collected" list for all counties + record types. (Separate session/PR.)

## PHASE B — SELECT (control), later
- [ ] Generalize the `doc_types` selection param + validation beyond pre_foreclosure, gated per (county, record_type) `supported_for_selection`.
- [ ] Per-county live-portal verification before flipping each `supported_for_selection=True`.

- [x] A4. Dataset connectors (king/pierce code_violation, king/snohomish tax) -> kind="dataset" + source note. Commit `f22af95`.
- [x] A5. API: `collection_scope_by_record_type` on ConnectorResponse + `connector_scraper_class()` resolver + populated in `list_connectors`. openapi.json hand-edited (pinned-venv convention, no local-regen drift). Commit `80f83e7`.
- [x] A6. SHOW/SELECT separation + connector-wiring guard tests. Commit `0982803`.
- [x] A7. Codex review of full diff (`origin/main`): P1 = Clark 257 behavior change (intentional/authorized fix, verified 0-impact — TRSL empty); P2 = Clark tax scope advertised unselected types -> FIXED `85a97af` (derive from checkbox selection), Codex-confirmed clean.
- [ ] A8. Frontend (separate repo `bridgeleads-web`): wizard renders read-only "Documents collected" per county+record_type from `collection_scope_by_record_type`. Separate PR. Run `npm run gen:api-types` after this backend merges (regen openapi in pinned `.venv-schema` first).

## Review

**Backend SHOW feature COMPLETE on `feat/doc-type-visibility` (8 commits, all ruff-clean, 12 unit tests, Codex-reviewed).**

What shipped:
- Every active connector now answers `collection_scope(record_type)` describing what it collects, surfaced via `GET /connectors` -> `collection_scope_by_record_type`. Purely additive (no scrape-behavior change) EXCEPT the separate, authorized Clark 257 fix.
- Honesty guarantees (Codex-reconciled): exact portal labels marked `exact=True` (king-NTS/pierce/clark/snohomish), keyword predicates `exact=False`, broad predicates -> "X-related filings", cryptic county codes -> explicit bucket, dataset connectors -> `kind="dataset"`, divorce from the shared classifier. Coverage test fails on any unmapped keyword.
- Clark investigation (the "investigate first" detour): root-caused the 6-vs-5 mismatch (missing checkbox 257), live-verified all IDs + that the portal filters server-side by codes (corrected a false code comment), confirmed bare DEFAULT(66) is an empty category. No production lead loss ever existed.

Remaining: A8 frontend (separate repo/PR). Branch not yet pushed / no PR opened — awaiting user go-ahead.

Deferred (noted, not this PR):
- Clark probate/divorce/tax checkbox-code completeness audit (server-side filtering makes the checkbox list load-bearing for all record types).
- Phase B (SELECT): generalize user doc-type selection beyond pre_foreclosure, county-by-county after live verification.
