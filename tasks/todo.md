# King tax-delinquent — replace party_name placeholder with real owner name

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
