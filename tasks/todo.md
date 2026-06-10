# Task: Leads have no phone/email — diagnose + remediate

## Diagnosis (DONE — Claude + Codex agree)
Root cause: **skip-trace was never enabled** on any of the 10 recent scraper configs.
`ScraperConfig.skip_trace_enabled` defaults False (`models.py:194`); the enqueue path
bails at `tasks.py:1344` when it's False → 0 rows ever entered `pending_skip_trace_rows`
→ 4,761/4,765 results stuck at `not_attempted`, no phone/email.
NOT a bug, NOT a credits problem (Tracerfy funded, dispatcher healthy). Opt-in by design.

Codex extras:
- No on-demand skip-trace API route, and **no UPDATE endpoint** to toggle skip-trace on an
  existing scraper (`scrapers.py` = create/get/delete only). Gap.
- Existing `scripts/sprint4_enqueue_existing.py` is hardcoded to thurston/kitsap/whatcom
  probate and ignores the per-config gate — unsafe for selected-job backfill.
- `pending_skip_trace_rows` has NO unique constraint on `result_id` → backfill must exclude
  result_ids already pending to avoid double-enqueue/double-charge.

## Decisions (user)
- Going forward: **build a proper per-scraper skip-trace toggle endpoint** (backend + UI).
- Existing leads: backfill, but **dry-run first** (real credit counts, no assumed $).
  Cost is Tracerfy account credits (~1–1.6 credits/lookup), not the user-facing $0.05/0.08 meter.

## Phase 1 — Safe backfill script (immediate pain)
- [x] Build `scripts/backfill_skip_trace_jobs.py`: explicit `--jobs` / `--hours`, dry-run
      default, `--commit` to write; ORM-based (auto-encrypt); cache-first; exclude already-pending
      result_ids; print per-job eligible / cache-hit(free) / would-enqueue(paid) + Tracerfy balance.
- [x] Ran DRY-RUN: 4,692 eligible, ~7,031 credits to trace all; Tracerfy balance only 564.
      Snohomish tax = 6,268 of it; all non-tax leads = ~763. 0 cache hits, 23 non-personal dropped.
- [~] DEFERRED by user ("i will do the skip tracing for all counties another time"). No commit run.
      Re-run later (after credit top-up): `... backfill_skip_trace_jobs.py --hours 36 --commit`.

## Phase 2 — Toggle endpoint (going forward)
- [~] Consult Codex on design — Codex RATE-LIMITED (usage cap, resets ~10:13 PM). Self-verified
      design against code instead (delete_scraper already UPDATEs under get_rls_db = precedent).
- [x] Backend: `ScraperConfigUpdate` schema + `PATCH /scrapers/{scraper_id}` route. Tenant-scoped
      (id + user_id, 404), plan-gated on SKIP_TRACE_ADDON_PLANS (same gate as create → not a weaker
      door), rate-limited, normal CurrentUser auth. Disabling never cancels queued traces.
- [x] Frontend: `updateScraperSkipTrace()` client + plan-gated toggle switch on each scrapers-list
      row footer (Pro+; Starter disabled w/ upsell; 402 → toast). `lib/api.ts`, `scrapers/page.tsx`.
- [x] Verify: backend ruff clean + py_compile OK; frontend `tsc --noEmit` exit 0 (no ESLint configured).
- [ ] **PENDING GATE: Codex review the diff** (`codex review`) once usage resets — NO-GO on any Crit/High.
- [ ] Live-verify endpoint (request/response) + deploy. UNCOMMITTED.

## Review
**Built (UNCOMMITTED, both repos):**
- Diagnosis: leads had no phone/email because skip-trace was never enabled on any of the 10 scraper
  configs (opt-in, defaults off). Not a bug, not credits. Claude + Codex agreed.
- `scripts/backfill_skip_trace_jobs.py` (+ 2 diag scripts) — safe dry-run-first backfill. Dry-run showed
  4,692 eligible / ~7,031 credits vs 564 balance. User DEFERRED the backfill.
- Skip-trace toggle endpoint (backend PATCH + frontend switch) so skip-trace can be flipped on existing
  scrapers — fixes the missing-update-endpoint gap.

**Remaining:** Codex review gate (rate-limited), live-verify, commit + deploy. Backfill deferred by user.
