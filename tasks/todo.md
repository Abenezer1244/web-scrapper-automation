# Phase 3 — Combine + Overlap (build)

**Branch:** `feature/phase3-combine-overlap`. **Migration head:** 036 → next 037.
**Design:** `docs/superpowers/plans/2026-06-04-phase3-combine-overlap.md` (Codex-reviewed).
**Goal of first slice:** ship **intersection** ("on both lists") export — strong-identity only.

## Foundation (verified, Phase 1 live)
- `property_identity.compute_property_key(parcel, addr)` → sha256(parcel|addr) or None (weak).
- `membership_query.users_overlap(user_id, types)` → property_keys on ALL ≥2 distinct types.
- `_upsert_property_membership` in tasks.py writes membership from post-enrichment `refreshed` rows.
- `Result` has NO `property_key` and NO `record_type`/`county` (those live on ScraperConfig via Job).
- Export/CSV pattern: `download_export` in jobs.py — `sanitize_for_csv` per field, `Response(text/csv)`.
- RLS: `get_rls_db` sets `app.current_user_id`; membership + results tables have USING policies.

## Slice 3A — `Result.property_key` data foundation (≤5 files)
- [ ] Migration **037**: `results.property_key` `String(64)` nullable + index `(user_id, property_key)`. Additive, no backfill in-migration.
- [ ] `models.py`: add `property_key` column to `Result` + index in `__table_args__`.
- [ ] `tasks.py`: populate `property_key` on the post-enrichment `refreshed` rows in the SAME loop as membership (reuse `_compute_property_key`), persist before/with the membership upsert. Failure-isolated like membership.
- [ ] `scripts/backfill_result_property_key.py`: offline, best-effort, batched, idempotent (mirror Phase 1 backfill; keyset by `r.id`).
- [ ] Verify: `python -c` compile, `alembic upgrade` dry logic review (no test DB here → Codex-verify + CI roundtrip).
- [ ] **Codex review of 3A diff** → reconcile → commit.

## Slice 3B — Intersection export endpoint (after 3A approved)
- [ ] `schemas.py`: request `{record_types:[...], counties?:[...]}`, response columns incl. `matched_record_types`, `overlap_count`.
- [ ] New route (e.g. `src/api/routes/segments.py`): POST intersection preview/export.
  - `users_overlap(record_types)` → property_keys.
  - Join `results r → jobs j → scraper_configs sc` on `r.property_key = ANY(keys)`, `sc.record_type = ANY(types)`, optional `sc.county = ANY(counties)`, `r.user_id = :uid`.
  - Representative row via ONE window function: rank by phone/email present, most recent job, `r.id` tiebreak.
  - `array_agg(DISTINCT sc.record_type)` → `matched_record_types`; `overlap_count`.
  - Tenant-scoped (RLS), `sanitize_for_csv` all fields, reuse CSV `Response` pattern.
- [ ] Register router in `main.py`.
- [ ] Tests: window-rank / overlap logic; DB roundtrip in CI.
- [ ] **Codex review of 3B diff** → reconcile → commit.

## Constraints
- No test DB / Playwright locally → build + Codex-verify + CI roundtrip.
- Migration 037 additive nullable = low risk. Merge → deploys on boot (advisory-lock migrate).
- Intersection = strong-only and SAY SO in API. Union (later slice) = inclusive.

## Codex collaboration log
- **Consulted on 3A plan (session 19011 follow-up). Reconciled — adopted all:**
  - (A) Persist via explicit **bulk UPDATE by id in its own transaction**; do NOT mutate the `refreshed` ORM objects (autoflush could push writes early / poison the shared session before the membership commit).
  - (B) Order: **CSV re-export → property_key write → membership upsert** (so 3B never sees membership without joinable result rows; both still failure-isolated, never fail a delivered job).
  - (C) Backfill **ALL** rows incl `is_duplicate=true` (property_key = identity, not visibility); skip only weak (`compute_property_key` → None).
  - (D) Use a **partial index** `(user_id, property_key) WHERE property_key IS NOT NULL` (smaller, null identities never queried).
  - (E) Live write `WHERE id=:id AND property_key IS NULL` (idempotent, never clobber). Log counts (computed/null/updated/failed). Backfill: keyset by id, batch commits, select only id/parcel/address. ResultRow schema is explicit → no API leak (verified). RLS: backfill mirrors Phase 1 (runtime role bypasses RLS).
  - **Decision (CONCURRENTLY):** Codex suggested `CREATE INDEX CONCURRENTLY`. REJECTED for this migration — it can't run inside the advisory-lock transaction `scripts/migrate.py` uses, and `results` is ~277K rows (precedent: migrations 033/034 added results indexes with a plain `op.create_index`). Plain index = brief lock, sub-second at this scale.
- **Codex review of 3A diff (gpt-5.5, `codex review --base HEAD~1`): GATE PASS** (no P1; one P2). "Live schema and worker changes are mostly sound."
  - **[P2] FIXED:** backfill seeded `last_id=""` for `WHERE id > :last_id`, but `results.id` is UUID → Postgres `invalid input syntax for type uuid: ""` on the first query (backfill dies before scanning). Fixed: seed nil-UUID `00000000-...-0` + explicit `CAST(:last_id AS uuid)`. Re-verified: compile + ruff clean.
  - **⚠️ Same latent bug in the Phase 1 twin `scripts/backfill_property_membership.py:28,40`** (`last_id=""` + `WHERE r.id > :last_id`). Out of 3A scope — flagged to user, one-line fix available.

## 3A status: ✅ BUILT + Codex-reviewed (gate pass, P2 fixed). Committed d2513dc.
## Phase 1 twin backfill UUID-cursor bug: ✅ FIXED (a68dbbf).

## Slice 3B — Intersection export endpoint: ✅ BUILT
- [x] `schemas.py`: `SegmentIntersectionRequest` (2+ distinct supported types, counties cleaned), `SegmentLeadRow`, `SegmentIntersectionResponse` (identity_strength="strong" — SAYS SO). `SUPPORTED_RECORD_TYPES` canonical set.
- [x] `src/api/routes/segments.py`: `POST /segments/intersection` (JSON preview, cap 500) + `POST /segments/intersection/export` (CSV, cap 50k). `users_overlap()` → 3-CTE query (candidates → agg(array_agg DISTINCT, count DISTINCT) → ranked(row_number window: contactable→recent→id)) → representative row + matched_record_types + overlap_count. RLS-bound + explicit user_id filter; `sanitize_for_csv` all fields; optional county clause (fixed fragment, bound params).
- [x] Registered in `src/api/__init__.py` + `main.py`.
- [x] `tests/test_segments_intersection.py`: 15 pure tests (validation + SQL assembly). DB roundtrip → CI (no test DB here).
- [x] Key design fidelity: array_agg(DISTINCT) is NOT a window aggregate in PG → split into agg + ranked CTEs. Intersection strong-only and says so.
- [x] **Codex review of 3B diff** — iterated to clean. Round 1 (gate pass) caught 2 P2: (1) county filter could return non-intersections → added `agg HAVING count(DISTINCT)=:n`; (2) keys materialized in Python before LIMIT → pushed overlap into a membership subquery (no Python key array, LIMIT bounds in-query, still membership-sourced per design). Round 2 caught 1 P2: closed `SUPPORTED_RECORD_TYPES` rejected `death_certificate` → replaced with shape validation (slug regex), matching the DB-driven `bound_record_types` convention. **Round 3: CLEAN** ("tenant-scoped, validated, bounded").

## ✅ Phase 3 first slice DONE — review

**Shipped (branch `feature/phase3-combine-overlap`):** intersection ("on both lists") export.
- 3A `d2513dc`: `Result.property_key` (migration 037, partial index) + post-enrichment stamp + offline backfill.
- twin `a68dbbf`: fixed identical UUID-cursor crash in Phase 1 membership backfill.
- 3B `fa132c1`: `POST /segments/intersection` (JSON preview) + `/export` (CSV); membership-sourced overlap computed in-SQL, representative row via window fn, `matched_record_types` + `overlap_count`, strong-identity-only (says so), tenant-scoped + CSV-sanitized + rate-limited.

**Verification:** 31 tests pass (17 new segment + 9 identity + 5 membership); ruff clean on all new files; `alembic heads` linear at 037; app builds with both routes registered. No DB roundtrip locally (constraint) → window-rank/overlap correctness deferred to CI.

**Security (non-negotiables):** every query filters `user_id` (explicit + RLS belt/suspenders; configs/jobs reached only via this user's results); `sanitize_for_csv` on all exported fields; Pydantic-validated input; rate-limited; no secrets; global handler returns ref-id not stack trace. Codex confirmed clean.

**Codex worked every step:** plan consult (adopted bulk-UPDATE/order/partial-index/backfill-all), 3A review (1 P2 fixed), 3B review (3 P2 fixed across 2 rounds, final clean).

## Slice 3C — Inclusive UNION ("combine") export: ✅ BUILT (`6e42182`)
- `POST /segments/union` (JSON preview) + `/union/export` (CSV + `identity_strength` col). 1+ distinct types.
- Dedup bucket `COALESCE(property_key, dedup_hash, 'id:'||id)`: strong→property_key, weak→dedup_hash, neither→singleton. NO pk:/dh: prefix (un-backfilled strong row coalesces by hash value — Codex). `identity_strength` per-bucket via `bool_or(property_key IS NOT NULL)`.
- Ranking: **contactable FIRST** → is_duplicate → recent → id (Codex P2: contactable-first so we never drop available phone/email; matches intersection). Never filters is_duplicate=false (would drop a lead).
- No membership (union = direct results scan). Explicit `j.user_id`/`sc.user_id` predicates (added to intersection too).
- 13 union tests. Codex review: 1 P2 (ranking order) fixed → **final CLEAN**.
- **Documented caveat (not hidden):** dedup_hash is PRE-enrichment, property_key POST — un-backfilled strong rows whose enrichment changed parcel/addr can split/mislabel until `backfill_result_property_key.py` runs. Weak dedup is name|date (not property) → weak buckets merge same-name/date across types (intended; overlap_count not overclaimed for weak).

**Pending / later slices (NOT built):** segment-builder UI (frontend repo `bridgeleads-web`); saved `Segment` model + scheduled combined delivery (Phase 5); optional perf index `(user_id, dedup_hash) WHERE dedup_hash IS NOT NULL` if heavy-user union scans show up. Migration 037 is branch-only — do NOT apply to prod until merged to main (migration/branch landmine). Run BOTH backfills offline post-merge.
