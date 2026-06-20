# Code Violation Scrapers — Hardening Pass (2026-06-20)

**Scope:** Every county with a `code_violation` connector. Confirmed via registry +
`county_connectors` audit: exactly **two** — `king` (Seattle SDCI / Socrata) and
`pierce` (Tacoma / ArcGIS). Both pure-HTTP (no Playwright).

**Goal (user ask):** verify each is legit/solid/hardened for multi-tenant use,
fix what's wrong, collaborate with Codex, cross-verify with orchestrated agents,
and live-test that both work.

## Live baseline (read-only, before any change)
`scripts/live_test_code_violation.py 30` vs REAL public APIs:
- King (Seattle Socrata): 1963 records/30d. red_flags: no_parcel 1963 (by design), no_address 1, **party_name pollution**.
- Pierce (Tacoma ArcGIS): 35 records/30d. red_flags all 0 — clean.

## Findings
- [ ] F1 HIGH (King) party_name PII pollution — falls back to raw free-text `description` (complainant narrative incl. disability disclosure, unbounded) when `recordtypedesc` empty; same text in enrichment_data. Fix: clean violation-type label + address only; drop complaint fallback; stop persisting narrative.
- [ ] F2 HIGH (both) false-empty reliability — both `break` on API error, return partial/empty → job marked DONE (silent truncation). Fix: bounded retry then RAISE (match king_wa_tax_delinquent / probate / preforeclosure).
- [ ] F3 MEDIUM (King) unstable Socrata pagination — `$order: opendate DESC` + offset skips/dupes rows. Fix: `$order: ":id"`.
- [ ] F4 MEDIUM (Pierce) date-window off-by-one — `opendate <= TIMESTAMP 'YYYY-MM-DD'` drops end-date records. Fix: end-of-day bound.
- [ ] F5 MEDIUM (Pierce) ArcGIS pagination robustness — honor `exceededTransferLimit`, order by unique objectid.
- [ ] F6 LOW (King) silent dateless drop + no zero-canary — add structural canary like King tax.
- [ ] F7 multi-tenant (both): PASS — stateless scrapers; isolation at worker/RLS layer. Document, no change.

## Plan
1. [ ] Consult Codex on findings before code (CLAUDE.md mandate).
2. [ ] Implement — one agent per scraper file (independent, 1 file each).
3. [ ] Cross-verify: code-reviewer agent + Codex review the diff.
4. [ ] Live-test both again; confirm clean party_name, fail-loud, sane counts.
5. [ ] ruff + tests. BUILD_JOURNAL entry. Review section.

## Review
_(to be filled in after implementation)_

## Review (completed 2026-06-20)
**Outcome:** Both code_violation scrapers (King/Seattle, Pierce/Tacoma) hardened + live-verified. NOT yet committed.

**Findings status:**
- F1 ✅ King party_name PII leak fixed (structured label only; description dropped from enrichment). Live-verified clean.
- F2 ✅ Both fail loud (bounded-retry-then-RAISE `_fetch_page`); ArcGIS 200-error-body detected + retryable marker.
- F3 ✅ King `$order=:id` stable pagination.
- F4 ✅ Pierce half-open UTC date window; epoch parsed tz=UTC.
- F5 ✅ Pierce exceededTransferLimit + objectid ordering.
- F6 ✅ Both: canary (≥100 fetched/0 emitted→raise) + max-page guard + date-skip counter/warning.
- F7 ✅ Multi-tenant PASS (stateless scrapers; isolation at worker/RLS).

**Cross-verification:** Codex consult (pre-code, caught 3 misses) + 2 impl agents (parallel) + code-reviewer agent (caught retryable-marker + bare-except, both adopted; 1 claim rejected) + Codex review ×2 = NO P1/P2.

**Reviewer findings reconciled:** R2 (Pierce bare except→log+count) ADOPTED; R3 (ArcGIS error-body retryable) ADOPTED; R1/R7 (date-skip counters) ADOPTED; R6 (stale comment) ADOPTED; R4 (King guard→top) ADOPTED; R5 (infinite-loop claim) REJECTED as false.

**Live evidence:** King 1963 records/30d (party_name clean), Pierce 35/30d (red_flags all 0). 3 runs, identical counts (no records lost from pagination changes). ruff clean, py_compile OK.

**Handoff:** commit + branch + PR + prod canary re-probe. King = Seattle-city scope only (separate product decision).
