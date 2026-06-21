# King tax-delinquent owner-name REACH fix (follow-up to PR #80)

> PR #80 (`d2253f6`) is MERGED — the owner-swap logic is correct and live-proven
> (6/6 parcels returned real owners on 2026-06-21). This follow-up makes the swap
> actually REACH existing leads. Original PR #80 plan archived in `tasks/todo.pr80.bak`.

## Problem (reach gap)
The swap (`enrich.py:396-401`) only runs inside the King eRealProperty pass, which
is gated to rows **missing a mailing address** (`enrich.py:354-360`). But:
1. `_reuse_enrichment_for_duplicates` COALESCEs `mailing_address` from the pre-fix
   delivered record onto duplicates → they have a mailing address → excluded from
   the King pass → placeholder survives.
2. King tax is a point-in-time snapshot; every parcel already exists, so a fresh
   job is ~100% duplicates → almost nothing gets the swap.
3. `_MAX_KING_PARCELS = 300` caps per-job lookups.
Result: UI still shows placeholders on existing ~28k King tax leads.

## Decision (user-approved 2026-06-21): widen the forward gate + backfill existing.

## Plan
### Phase 1 — Forward gate (`src/workers/tasks_helpers/enrich.py`) [1 file + tests]
- [ ] Compute `is_tax_delinquent` + import `is_tax_placeholder_party` before `needs`.
- [ ] Widen `needs` to also include tax_delinquent placeholder-named rows even when
      `mailing_address` is present.
- [ ] When capping to `_MAX_KING_PARCELS`, prioritize placeholder-named parcels.
- [ ] Test: tax row WITH mailing + placeholder name gets its owner swapped.

### Phase 2 — Owner-only helper (`src/scrapers/enrichment/king_county_assessor.py`) [1 file + tests]
- [ ] `batch_extract_king_owners(parcel_ids) -> dict[str,str]`: Phase-1-only HTTP
      (reuses `_extract_owner_name`, no Playwright) for the backfill.
- [ ] Tests against real markup (no mocks).

### Phase 3 — Backfill (`scripts/backfill_king_tax_owner_names.py`) [1 file]
- [ ] Select `results` with placeholder `party_name` + parcel_id, joined to
      `scraper_configs` for county=king/state=wa/record_type=tax_delinquent.
- [ ] parcel→owner map via `batch_extract_king_owners`; UPDATE only still-placeholder
      rows (idempotent), batched commits, dry-run flag, loud summary.
- [ ] Owner is public parcel-keyed data → safe across tenants; row updated under its
      own user_id.

### Gates
- [ ] Codex consult BEFORE coding · ruff clean · pytest green · Codex review+challenge
- [ ] Security Master Review (§14): multi-tenant, SSRF (safe_get), no PII in logs
- [ ] New PR; do not push until user approves.

## Review (2026-06-21)
All three phases implemented on branch `fix/king-tax-owner-reach`, verified, Codex-clean.

**Changes**
- `src/scrapers/enrichment/king_county_assessor.py`: new `batch_extract_king_owners()`
  (HTTP-only owner lookup, no Playwright) + `_fetch_king_owner()` (bounded retry on
  transient failure via `Settings.MAX_RETRIES`, distinguishes genuine 200-miss from
  transient error). Numeric-parcel guard.
- `src/workers/tasks_helpers/enrich.py`: new owner-only forward pass in the King block —
  resolves owners for tax_delinquent rows that have a mailing address but still a
  placeholder name (the dedup-reuse case the missing-mailing pass skipped). 500-cap with
  non-silent overflow log; commit-honest success/failure logging (guarded post-commit log).
- `scripts/backfill_king_tax_owner_names.py`: idempotent, re-runnable backfill of existing
  leads. Config-join + placeholder-shape scope; global parcel→owner cache; still-placeholder
  UPDATE guard; `--dry-run/--batch/--limit` (limit applied to SELECT).
- `tests/test_king_assessor_owner.py`: +3 tests (helper guard/dedup/numeric); 27 pass total.
- `pyproject.toml`: per-file S608 ignore for the backfill (generated-:param SQL, values bound).

**Verification**
- ruff clean; 27 targeted tests pass.
- LIVE: `batch_extract_king_owners` returned real owners for the exact parcels that showed
  placeholders (AL-SABAH JABER / CWIAK KATHLEEN L / RIAN SKYE GOOD LEWIN); short/blank/
  non-numeric skipped with zero requests.
- LIVE dry-run vs prod DB: scope join finds 213,326 in-scope placeholder rows; bounded
  dry-run scanned 20 (precise --limit), 19 would-attempt, 1 correctly rejected as
  not-exact-placeholder, ROLLBACK (no writes).
- Codex: consult (pre-build) + review + 2 re-reviews → final CLEAN, no P1.
- Security §14 non-negotiables: multi-tenant / SSRF / CSV / secrets / PII-in-logs all PASS.

**NOT done (needs user decision)**
- [ ] Commit + open PR (not pushed).
- [ ] Run the backfill in PROD (213k-row mutation) — only dry-runs executed so far.

---
## (ARCHIVED) PR #80 — replace party_name placeholder with real owner name

Branch: `fix/king-tax-owner-name`

## Problem
King tax-delinquent leads ship to users with `party_name =
"Tax Delinquent — $X owed (Parcel …)"` — a placeholder, never a person.
- King's Socrata source (`dsv3-ct3e`) has no owner column, so the placeholder is
  correct AT SCRAPE TIME (`king_wa_tax_delinquent.py:248`).
- The docstring (`king_wa_tax_delinquent.py:29`) claims the name is "enriched
  downstream" — but the enrichment (`enrich.py:354`, `king_county_assessor.py`)
  only extracts `property_address` + `mailing_address`. It NEVER reads the owner
  name and NEVER writes `party_name`. So the placeholder is permanent.
- Snohomish is unaffected — its bulk file already carries the owner (field 7).

## Verified facts
- eRealProperty Dashboard page (already fetched in Phase 1 HTTP for the property
  address) DOES carry the owner, markup `<td ...>Name</td><td>VALUE`.
- `Name</td>` appears EXACTLY ONCE on the page → safe, unique regex.
- Example: parcel `1954600115` → `TOMLINSON WILLIAM+CHERYL L` (King joins
  co-owners with `+`; entity owners e.g. LLC/bank are valid tax-delinquent leads).

## Plan (small, gated, no extra requests)
- [x] 1. `king_county_assessor.py` Phase 1: extract owner via pure helper
      `_extract_owner_name(html)` (tolerant regex + `html.unescape` + tag-strip +
      junk rejection). `owner_name` added to result dict; row created when prop OR
      tax_url OR owner.
- [x] 2. `enrich.py` King block: overwrite party_name under DUAL gate —
      `config.record_type == "tax_delinquent"` (belt) AND
      `is_tax_placeholder_party()` (suspenders). Probate/death-cert King rows on
      the same path untouched; capped/missed parcels keep the labeled placeholder.
- [x] 3. Shared `tax_placeholder_party` / `is_tax_placeholder_party` predicate
      co-located with the producer in `king_wa_tax_delinquent.py` (anchored regex,
      can't drift). Tests: `_extract_owner_name` (10 cases) + predicate roundtrip
      (real markup, no mocks, no network).
- [x] 4. ruff clean; 24 targeted tests pass; verified on 3 LIVE King parcels.

## Codex collaboration
- [x] Consulted Codex on the plan BEFORE coding — tightened regex, gate, junk check.
- [x] Codex reviewed the diff x3: round 1 found 2 Medium + 2 Low (all adopted),
      round 2 found 1 residual edge (predicate not exact-shape), round 3 CLEAN.
      No Critical/High at any point.

## Review
**What changed (3 source files + 2 test files):**
- `src/scrapers/enrichment/king_county_assessor.py` — new `_extract_owner_name()`;
  Phase 1 now reads the owner off the SAME eRealProperty page already fetched for
  the address (zero extra HTTP). Returns `owner_name` in the result dict.
- `src/scrapers/king_wa_tax_delinquent.py` — placeholder now built by
  `tax_placeholder_party()`; new `is_tax_placeholder_party()` anchored-regex matcher
  (requires the `$amount` clause, so a real name starting with the prefix is never
  clobbered; dash-encoding-agnostic).
- `src/workers/tasks_helpers/enrich.py` — King enrichment block swaps the
  placeholder for the real owner under the dual gate.
- `tests/test_king_assessor_owner.py` (new, 10) + `tests/test_king_tax_delinquent.py`
  (+3 predicate tests).

**Proof:** live extraction verified —
`1954600115 → TOMLINSON WILLIAM+CHERYL L`, `4023500466 → AMES WILLIAM E & CHAMBERS G`,
`7941110080 → KALLEM SWARAJ & JYOTHI`.

**Behavior:** King tax-delinquent leads now show the real owner once enriched.
Misses/capped parcels keep the labeled placeholder (never blank). Snohomish
unchanged. Multi-tenant isolation unchanged (edit stays within the job's pid_map;
Codex confirmed no tenant-leak path).

## Out of scope
- Snohomish (already correct). Skip-trace (separate opt-in). The 300-parcel
  enrichment cap (pre-existing; capped rows keep the labeled placeholder).
