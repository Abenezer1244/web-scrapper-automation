# Snohomish tax_delinquent repair, Phase 1 (2026-09-13)

Source: `docs/audits/snohomish-coverage-audit-2026-09-13.md` (audit worktree). User approved the recommended order; this is step 1.

## Measured before coding (live source, aggregates only)
- Landing page now links the data file ABSOLUTELY (`https://www.snohomishcountywa.gov/DocumentCenter/View/151113/..._41.txt`, as-of `20260901`, 15 fields) and the field-description twin RELATIVELY (`/DocumentCenter/View/148137/..._36.txt`, as-of `04/21/2026`, 17 fields). `_DOC_LINK_RE` matches only relative hrefs, so the selector's last-resort fallback returns the twin: the scraper ingests April data.
- `tax_cap_min_year` returns the current year from Aug 1; parser keeps only years < as-of year; every parcel capped out Aug 1 to Dec 31, scraper raises.
- Current file: parcels by oldest unpaid year 2024=1,110, 2025=1,751. Current-year rows are ambiguous (244k parcels show owed == half levy in September), so current-year delinquency is NOT inferred.

## Todo
- [x] `_DOC_LINK_RE` accepts host-pinned absolute or relative DocumentCenter links; selector never returns the description twin (raise instead)
- [x] Staleness guard: parse the as-of DATE by consensus; raise when older than 62 days or in the future
- [x] `tax_cap_min_year` floored at `today.year - 1` (the most recent completed tax year is never capped)
- [x] Snohomish `date_recorded = None` (no fabricated January 1st; matches King #210)
- [x] Tests: link selection on the real current page shape, twin-only page raises, stale/future as-of raises, cap floor across all 12 months, date_recorded None
- [x] ruff clean; 639 passed across the 29 tax/Snohomish test files; full suite 2858 passed (7 pre-existing local failures also on main + 1 shared-Redis flake)
- [x] Codex diff review + §14 security pass
- [x] Measure fixed parser against the live file (expected about 1,751 parcels)

Files (4): `src/api/tax_filters.py`, `src/scrapers/snohomish_wa_tax_delinquent.py`, `tests/test_snohomish_tax.py`, `tests/test_tax_filters.py`.

## Decisions and disagreements
- Codex option A adopted for the cap. The May 1 anchor for `months_delinquent` display and filter math is DEFERRED to Phase 1b: it changes King's displayed months too and needs user sign-off.
- Codex P1 "use America/Los_Angeles for the cap year": NOT adopted in this phase. The cap `today` is UTC across 8 surfaces by a stated parity invariant (`lead_signals.py:97`). With the floor, the UTC effect is that the intended January 1 transition happens up to 8 hours early (Dec 31 PT evening), a loud scraper failure for any run in that window, never silent wrong data. Revisit with Phase 1b.
- Codex P1 stale source: covered by the new as-of staleness guard. Between January 1 and the county's first new-year file, the scraper fails loudly (0 parcels) rather than widening the cap.

## Out of scope (later phases, need approval)
Phase 2 NTS silent-empty health and connector-health ordering; Phase 3 Tracerfy over-quota dispatch; mailing-source license decision (user/legal).

## Review (2026-09-13)
- Two real defects, both proven on live county files: the cap floor (0 -> 1,751 parcels) and the stale-twin link (April file now rejected).
- Added after Codex r1: date-level whole-file as-of check, pure `_validate_parsed`. After r2: honest drift warning. Both gates PASS.
- Files: 4 code/test files + this plan + BUILD_JOURNAL entry. Not committed: waiting for owner approval.
- Deferred: Phase 1b (May 1 anchor, changes King display), Phase 2, Phase 3.
