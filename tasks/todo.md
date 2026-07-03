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
- [x] **P5** FE bridgeleads-web (worktree fe-auction-leads, off origin/master): RecordType union + "Auction Leads" label (wizard/records/coverage) + Pro unlock + segments/admin pickers. tsc+eslint clean.
- [ ] **P6** deploy api+worker · migrate via scripts/migrate.py · live Pierce e2e (👤 ops, after PR merge)
- [x] Dead-code sweep — F-rules clean on all touched BE files; helper reused (no dupes); FE tsc noUnused clean

## Codex review rounds (each caught a real issue)
- BE R1: unused datetime import + lint (P2) → fixed.
- BE R2: fingerprint collision, blank first-deliverable, unparseable date (3×P2) → fixed.
- BE R3: dedup identity (P2, user→parcel-based) + stale entitlement/catalog tests (P2) → fixed.
- BE R4: same-parcel double-bill (P2) → finalizer collapse.
- BE R5: connectors seeded 'unknown' hidden from picker (P2) → seed 'healthy'.
- FE R1: segments + admin connector hard-coded pickers omit trustee_sale (P2) → added.
- P1 "seed connectors" recurred every round until P4 landed (expected sequencing).

## Decisions logged (Codex-reconciled)
- Finalizer runs BEFORE billing (not beside pre_foreclosure hook, which is post-billing) so a fail-closed raise never strands a charge; on failure release dedup claims + _fail_job (mirrors R2-upload-failure handler).
- Fail-closed contract = auction_date + nts_notice_id ONLY (= is_valid_nts). default_amount/trustee are nullable throughout the NTS system (crawler + _write_match) → excluded, render "—" like pre_foreclosure. (Codex P2; kept by is_valid_nts precedent.)
- dedup_hash FROZEN (parcel|address billing key) — NOT changed. raw_html_hash set to per-notice sha256(source|ts_number)[:32] to stop insert-fingerprint collisions.
- Backend commits: fc8a638, 3171e5b, b04eb52, 8f009a7, 0345d77, bd0d7f2, 84dce9d.

## Follow-up (noted, NOT this build)
- ⏭️ **Expand Auction Leads to ALL pre_foreclosure counties.** Every county where we already scrape `pre_foreclosure` is a candidate for `trustee_sale`, but each needs its OWN NTS crawler feeding `nts_notices` (one per county's legal-notice paper; many paywalled / bot-blocked). Ship P/S/K first; add a county by (a) building its NTS crawler, (b) adding a thin subclass + connector seed row.

## Review
**Status: BE + FE code complete, all local checks green. Codex-gated except the last 2 trivial fixes (rate-limit; re-run after 16:44 PDT).**

- **Backend** (`feat/trustee-sale-record-type`, ~947 LOC / 16 files, 47 tests green, Ruff clean): record type + scraper + fail-closed finalizer (before billing) + lean export + has_auction_data + migration 081 (seed 3 connectors, healthy) + Pro plan/catalog.
- **Frontend** (`feat/auction-leads-record-type`, 5 files, tsc+eslint clean): "Auction Leads" everywhere (wizard/records/coverage/segments/admin) + Pro unlock. Auction columns reuse the has_auction_data path (no change).
- **Codex loop:** 1 pre-code consult + 6 diff reviews; every finding fixed from root or reconciled with a documented reason (is_valid_nts, frozen dedup). See the review-rounds list above.
- **Key decisions:** finalizer runs before billing (no stranded charge); fail-closed on auction_date+nts_notice_id only (= is_valid_nts); parcel-based dedup (user call) with same-parcel collapse enforced in the finalizer; connectors seeded 'healthy' for immediate visibility.
- **NOT pushed / no PRs yet** — awaiting user go-ahead. Phase 6 (deploy + migrate + e2e) is ops, post-merge.
- **Landmine:** OneDrive-shared repo — used additive worktrees only; do NOT delete/force-move branches.
