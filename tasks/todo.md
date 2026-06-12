# Fix: Lists overlap property_key — parcel-primary, county-scoped identity

**Bug (live, proven):** `compute_property_key` hashes `parcel|address` together; tax pipelines
store situs WITH city+ZIP4, GIS enrichment stores street-only → same parcel, different keys →
tax_delinquent NEVER overlaps recorder lists (39k King tax + King probate, zero overlap).

## Design (to be Codex-reconciled)

**1. SPLIT identity from dedup (the billing landmine).** `_compute_dedup_hash`'s strong branch
currently IS `compute_property_key` (tasks.py:598-648), and dedup_hash keys `delivered_records`
(the billing/duplicate claim). Dedup semantics must NOT change: keep the OLD `parcel|address`
formula for dedup_hash + the enrichment-reuse comparison (tasks.py:1138) — extract it as
`legacy_strong_signature()` so the old formula stays importable and the lockstep comment becomes
an explicit divergence note. Billing/dedup behavior: byte-identical.

**2. New `compute_property_key(parcel_id, property_address, county, state)`** (identity only):
- parcel branch (parcel_ok): sha256 of `P|{STATE}|{COUNTY}|{parcel}` — parcel ALONE decides
  identity (the bug fix), but **county+state-scoped** (🔑 Codex's bare-parcel suggestion
  collides across counties: same parcel number in king + pierce = false overlap) and
  branch-prefixed (a parcel string can't collide with an address string).
- address branch (no parcel, addr_ok): sha256 of `A|{STATE}|{COUNTY}|{addr}` — also fixes the
  pre-existing street-only cross-city collision ("8021 188TH ST SW" exists in many cities).
- weak: None (unchanged).
- normalize_parcel: also strip leading zeros? AGENT HYPOTHESIS — unverified; ask Codex.
  Risk: distinct parcels "0012"/"12" merging. Lean NO unless Codex argues otherwise.

**3. Callsites:** membership writers (tasks.py:164, 246) + result-insert key computation pass
county/state from the config they already have in scope. Segments/batch_export only READ
r.property_key — fine post-backfill.

**4. Backfill (no schema change, no migration):** `scripts/backfill_property_keys.py`:
- Recompute `results.property_key` for ALL rows (new scheme; county/state via jobs→scraper_configs
  join; keyset-batched like backfill_result_tax_fields).
- REBUILD `property_list_membership` from results (per-user delete+reinsert; recompute
  sighting_count/first_seen/last_seen from results aggregates — membership lacks county so its
  rows can't be re-keyed in place).
- Deploy choreography: merge → run backfill immediately. Between deploy and backfill, overlap is
  degraded (mixed schemes) but nothing 500s; dedup unaffected throughout.

**5. Union view:** `COALESCE(property_key, dedup_hash, id)` bucketing — after full backfill every
strong row has a new-scheme property_key, so strong rows bucket consistently; dedup_hash only
buckets weak rows (pre-existing caveat narrows).

## Phases (≤5 files, Codex-gated)
### Phase 1 — identity split + new scheme
- [ ] property_identity.py: new compute_property_key(+county/state), legacy_strong_signature kept
- [ ] tasks.py: dedup_hash + reuse-check use legacy formula; membership/result writers use new key
- [ ] Tests: same-parcel-different-address-format → SAME key; cross-county same parcel → DIFFERENT;
      dedup_hash unchanged for identical inputs (golden values); reuse-check unaffected
- [ ] Codex gate
### Phase 2 — backfill + prod run
- [ ] backfill_property_keys.py (results recompute + membership rebuild, dry-run default)
- [ ] Merge → deploy → run backfill → verify counts
### Phase 3 — verify
- [ ] King tax×probate overlap > 0 (diag_overlap.py); 155 deathcert×probate preserved (re-keyed);
      Lists UI intersection shows tax pairs

## Review
_(end)_
