# Phase 3 — Probate TOD customer toggle (backend)

Branch `feat/probate-tod-toggle` (worktree `.claude/worktrees/probate-tod-toggle`),
stacked on `feat/probate-tod-signal` (PR #115, off origin/main `17ddfd1`).

## Goal
Let a customer include/exclude LIVING-OWNER Transfer-on-Death deeds from a probate
scraper's output. Death-triggered TOD always stays. Within the probate entitlement —
no new record_type, no new product.

## Design (RECONCILED with Codex — high-effort consult, it read the live code)
Codex materially improved the handoff plan. Adopted in full (Codex wins; docs silent):

1. **Do NOT reuse `doc_types`** (recorder-side portal-token registry). Add a dedicated
   tri-state config field **`include_living_owner_tod: bool | None`**:
   - `None`  = legacy / grandfathered → INCLUDE TOD (but Phase 2 already labels it).
   - `False` = new probate default → EXCLUDE living-owner TOD.
   - `True`  = explicit opt-in → INCLUDE TOD.
2. **Enforce ONCE at the worker chokepoint**, not per-scraper, and **before EVERY
   downstream consumer** — not just `rows.append`. Codex caught the leak: the first R2
   export is built from the in-memory `records` list (`tasks.py:863`), NOT persisted
   `results`. Filter `records` → `kept_records` early (before validate/insert/export/
   counts/dedup), so insert + export + counts + dedup + enrichment + billing +
   membership all see the filtered set.
3. **Use `classify_probate_signal_for_row(doc_type, comment)` and drop only when the
   result `is ProbateSignal.TOD_LIVING_OWNER`** — NOT the weaker `is_living_owner_tod(doc_type)`,
   because a recorder comment can upgrade a TOD deed into a death-triggered (real) lead.
4. Defaults: create / preview / batch-create write explicit `False` for NEW probate
   configs; **PATCH preserves omitted `None`** (editing an old config must not silently
   flip TOD off). Slug is `tod_living_owner_estate_planning`.
5. Dedup unchanged (no new product → property-level dedup is correct; lead_subtype stays
   out of dedup_hash).

## Phasing (each ≤5 files, each independently shippable)

### Phase 3a — data model + worker enforcement (core honesty filter)  [zero behavior change]
With no API writing the column, every config is `NULL` → include → identical output.
Safe to merge/deploy before the frontend.
- [ ] `src/db/models.py` — add `include_living_owner_tod = Column(Boolean, nullable=True)`
- [ ] `alembic/versions/071_scraper_config_include_living_owner_tod.py` — nullable bool (down_revision `070`)
- [ ] `src/scrapers/probate.py` — pure predicate `should_include_probate_row(record_type, include_living_owner_tod, doc_type, comment) -> bool`
- [ ] `src/workers/tasks.py` — build `kept_records` (probate + flag is False), call predicate, log dropped count; reassign before validate/insert/export
- [ ] `tests/test_probate_tod_filter.py` — predicate truth table (None/False/True × death/TOD/death-triggered-TOD/comment-upgrade)
- [ ] Verify (synthetic-env importlib, NO bare pytest), ruff, then `codex review` the 3a diff

### Phase 3b — API surface (lets customers choose)  [behavior change: new probate default = TOD off]
- [ ] `src/api/schemas.py` — `include_living_owner_tod: bool | None` on ScraperCreate, response, batch-create, preview
- [ ] `src/api/routes/scrapers.py` — create defaults NEW probate→False; validate (probate-only); PATCH preserves None; expose in response
- [ ] `src/api/routes/batches.py` — batch-create defaults new probate children→False
- [ ] tests for default-on-create + PATCH-preserve-None
- [ ] Verify, ruff, `codex review` the 3b diff. Backend-first; FE regenerates OpenAPI types.

### Phase 4 — frontend (SEPARATE repo `bridgeleads-web`) — NOT this session.

## Safety / gotchas
- Shared OneDrive repo: never delete/force-move branches; additive worktree + push only.
- conftest teardown WIPES prod tables + .env=PROD → NEVER bare `pytest`; test pure
  predicate via importlib under synthetic env (as Phase 1+2 did).
- Codex pre-build consult: DONE, clean (no Critical/High; endorsed central enforcement).
- Codex reviews each phase diff AFTER. Any Critical/High = NO-GO.
- Do NOT touch `docs/BUILD_JOURNAL.md` (concurrent-session clobber). Use memory.

## Open decision for the user
New DB column + migration 071 (chosen — cleanest tri-state) vs stuffing the flag into an
existing JSON column to avoid a migration. Going with the column unless you object.
