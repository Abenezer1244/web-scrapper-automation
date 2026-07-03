# Auction Leads — new record type `trustee_sale`

Branch: `feat/trustee-sale-record-type` (worktree `bridgeleads-worktrees/auction-leads`, off `origin/main` e3424e8).
Turn `nts_notices` rows into a deliverable lead list, reusing scrape→results→delivery.
Counties: **Pierce, Snohomish, King** only (the only 3 with NTS data). Plans: Pro / Business / Agency.

## Verified design (file:line, not assumed)
- Scraper base = `BridgeScraper`; DB-backed precedent = `snohomish_wa_pre_foreclosure.py` (no-op `__aenter__/__aexit__`).
- Worker `_run_scraper` (enrich.py) passes only `record_type`/`doc_types` to ctor, NOT `county` → **thin per-county subclasses**.
- `ScrapedRecord` has NO auction_date/default_amount/nts_notice_id. Persistence (tasks.py:753-781) passes `enrichment_data` through unchanged, never sets typed auction cols.
- Typed auction cols ONLY written by `nts_matcher_task._write_match`, gated to `record_type=="pre_foreclosure"` (fuzzy parcel/address match). For trustee_sale the source IS `nts_notices` → populate DIRECTLY by known notice id via a dedicated finalizer.
- Registry DB-driven; `scraper_mode="manual"`, `render_mode="static"`; migration head = **080** → chain **081**.
- `RECORD_TYPES_BY_PLAN.PRO` does NOT inherit ALL → add `trustee_sale` to Pro explicitly.

## Codex (consulted, high reasoning) — endorsements + HIGH finding
- Deterministic post-persist finalizer keyed on notice_id = correct (not fuzzy matcher; not generic insert-mapper coupling).
- **HIGH: finalizer must FAIL-CLOSED** — require `nts_source.notice_id`, populate all typed cols, assert 0 trustee_sale results with NULL auction_date/nts_notice_id before delivery, else fail the task. (We SELECT only `auction_date>=today`, so a NULL result = pipeline broke, not bad data.)
- Sync-in-async scrape() OK (Celery worker, small indexed query, matches Snohomish precedent).
- Watch within-tenant dedup: parcel-primary hash could collapse 2 distinct sales on same parcel → verify dedup identity includes `ts_number`.
- Thin subclasses confirmed; do NOT change `_run_scraper` contract.

## Phases (≤5 files each, Codex review each vs origin/main)
- [x] **P1** constants.py (ALL_RECORD_TYPES + Pro) · registry.py (allowlist) · NEW trustee_sale.py (base + 3 subclasses) · tests — Codex clean (P2 lint fixed)
- [x] **P2** NEW trustee_sale_finalize.py (fail-closed) · tasks.py hook (BEFORE billing) · 3 Codex P2s fixed (fingerprint/first-deliverable/date) · finalizer contract reconciled w/ is_valid_nts
- [x] **P3** lead_export.py `_TYPE_EXTRA_COLUMNS["trustee_sale"]` · jobs.py has_auction_data · tests
- [x] **P4** alembic 081 seed 3 county_connectors (manual/static, single head off 080) · migration-integrity test
- [ ] **P5** FE bridgeleads-web (own worktree): RecordType union + RECORD_TYPE_LABELS "Auction Leads"; verify wizard record-type source
- [ ] **P6** deploy api+worker · migrate via scripts/migrate.py · live Pierce e2e (👤 ops, after PR merge)
- [ ] Dead-code sweep

## Decisions logged (Codex-reconciled)
- Finalizer runs BEFORE billing (not beside pre_foreclosure hook, which is post-billing) so a fail-closed raise never strands a charge; on failure release dedup claims + _fail_job (mirrors R2-upload-failure handler).
- Fail-closed contract = auction_date + nts_notice_id ONLY (= is_valid_nts). default_amount/trustee are nullable throughout the NTS system (crawler + _write_match) → excluded, render "—" like pre_foreclosure. (Codex P2; kept by is_valid_nts precedent.)
- dedup_hash FROZEN (parcel|address billing key) — NOT changed. raw_html_hash set to per-notice sha256(source|ts_number)[:32] to stop insert-fingerprint collisions.
- Backend commits: fc8a638, 3171e5b, b04eb52, 8f009a7, 0345d77, bd0d7f2, 84dce9d.

## Follow-up (noted, NOT this build)
- ⏭️ **Expand Auction Leads to ALL pre_foreclosure counties.** Every county where we already scrape `pre_foreclosure` is a candidate for `trustee_sale`, but each needs its OWN NTS crawler feeding `nts_notices` (one per county's legal-notice paper; many paywalled / bot-blocked). Ship P/S/K first; add a county by (a) building its NTS crawler, (b) adding a thin subclass + connector seed row.

## Review
_(filled at end)_
