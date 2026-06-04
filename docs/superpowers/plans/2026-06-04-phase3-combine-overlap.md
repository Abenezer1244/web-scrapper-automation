# Phase 3 — Combine + Overlap: Design & Plan

**Status:** Designed (Claude + Codex, session 19011). Not yet implemented.
**Branch:** `feature/phase3-combine-overlap`. **Migration head:** 036 → next is 037.

## Goal
- **Intersection** ("on both lists"): properties on 2+ record types (e.g. probate ∩ pre_foreclosure) = highest-motivation sellers. **Strong-identity only.**
- **Union** ("combine"): merge multiple list types into one export. **Inclusive** — strong rows deduped by `property_key`, weak rows by `dedup_hash`, never silently dropped.

## Foundation (Phase 1, live)
`property_list_membership(user_id, record_type, property_key, parcel_id, property_address, ...)`; `users_overlap(user_id, record_types)` → property_keys on ≥2 types. `Result` has no `property_key` (only pre-enrichment `dedup_hash`).

## The hard problem + fix (Codex A)
`users_overlap` returns `property_key`s, but exporting leads needs full `Result` rows. `Result.dedup_hash` is **pre-enrichment** so it can differ from membership's **post-enrichment** `property_key`. **Fix: add `Result.property_key`** computed in the SAME post-enrichment spot as the membership write (`tasks.py`, reuse `compute_property_key`), indexed `(user_id, property_key)`. Backfill best-effort (nullable). Join must constrain by selected `record_types` (user_id+property_key alone can pull an unrelated newer row).

## Key decisions (Codex)
- **Representative row per property** via ONE SQL window function (no N+1): rank by (1) selected record types, (2) phone/email present, (3) most recent job, (4) `Result.id` tiebreak. Don't blind-merge names/addresses.
- **On-demand, no saved `Segment` table** yet (add only when scheduled combined-delivery exists). First slice = a preview/export endpoint with params `{record_types:[...], mode: union|intersection, counties?:[...]}`.
- **Export columns:** `matched_record_types` (array_agg distinct), `overlap_count`, `identity_strength` (strong|weak, union only).
- **Intersection strong-only and SAY SO** in API/UX. **Union inclusive** (strong by property_key + weak by dedup_hash).
- Biggest risk: identity semantics leaking into user trust — "combined"/"overlap" must not silently drop weak rows or mix unrelated result rows.

## Smallest first slice (Codex E) — ship intersection first
1. **`Result.property_key`** nullable column (migration 037) + populate in `tasks.py` post-enrichment (reuse `compute_property_key`, same loop as membership) + index `(user_id, property_key)`.
2. **Best-effort backfill** script (offline, like Phase 1's).
3. **Intersection export endpoint** — strong-only: `users_overlap(record_types)` → join `Result` on `(user_id, property_key, record_type ∈ selected)` → representative row via window function → export with `matched_record_types` + `overlap_count`.
4. Reuse `DataExporter`; tenant-scoped (RLS); CSV-injection sanitized.
5. Tests: registry/window-rank pure where possible; DB roundtrip via Codex oracle + CI.

## Then (later slices)
- Inclusive **union** export (strong+weak) with `identity_strength`.
- UI (frontend repo): segment builder (pick types, union vs "on both lists", overlap badge).
- Saved `Segment` model + scheduled combined delivery (ties into Phase 5 automation).

## Constraints
No test DB/Playwright here → build + Codex-verify + offline-render; DB roundtrip in CI. Migration 037 = additive nullable (low risk). Merge = deploy on boot (per workflow).
