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

### Phase 3a — data model + worker enforcement (core honesty filter)  [zero behavior change]  ✅ DONE (commit 745da95)
With no API writing the column, every config is `NULL` → include → identical output.
Safe to merge/deploy before the frontend.
- [x] `src/db/models.py` — add `include_living_owner_tod = Column(Boolean, nullable=True)`
- [x] `alembic/versions/071_scraper_config_include_living_owner_tod.py` — nullable bool (down_revision `070`)
- [x] `src/scrapers/probate.py` — pure predicate `should_include_probate_row(record_type, include_living_owner_tod, doc_type, comment) -> bool`
- [x] `src/workers/tasks.py` — filter `records` (probate + flag is False) before quota-cap/validate/insert/export; log dropped count
- [x] `tests/test_probate_tod_filter.py` — 13 cases (None/False/True × death/TOD/death-triggered-TOD-via-comment/unknown), all green
- [x] Verify: 13/13 predicate + 78 existing probate tests pass (synthetic-env importlib); ruff clean; compiles. `codex review` = GATE PASS, zero findings.

### Phase 3b — API surface (lets customers choose)  [behavior change: new probate default = TOD off]
- [x] `src/scrapers/probate.py` — shared pure `new_probate_config_tod_default` (no create/preview/batch drift)
- [x] `src/api/schemas.py` — `include_living_owner_tod: bool | None` on Create, Response, Update, BatchCreate
- [x] `src/api/routes/scrapers.py` — create+preview default NEW probate→False via shared helper; `_validate_tod_toggle` (probate-only 422); PATCH preserves omitted None + validates + audits + applies; Response exposes it
- [x] `src/api/routes/batches.py` — probate children default→False (non-probate children stay None)
- [x] `tests/test_probate_tod_filter.py` — +5 default-helper cases (probate None→False, explicit honored, non-probate→None passthrough)
- [x] Verify: ruff clean; route modules import OK; 24/24 tests pass.
- [x] `codex review` 3a = GATE PASS (zero findings). 3b = 1×P2 (PATCH explicit-null re-grandfathers
      a False config → re-enables TOD without opt-in). FIXED in `10e3300` via pure
      `effective_tod_on_update` (explicit null treated as omitted) + 6 targeted tests.
- [x] CONFIRMING codex re-review of the P2 fix (`--base 20d0d79`) = CLEAN: "did not find any
      introduced correctness issues." Phase 3 review gate satisfied — no outstanding Critical/High/P2.

## Commits (branch feat/probate-tod-toggle, NOT pushed, stacked on #115)
- 745da95  Phase 3a — column + migration 071 + worker filter + predicate
- 20d0d79  Phase 3b — API surface (create/preview/PATCH/batch)
- 10e3300  fix — PATCH explicit-null re-grandfather (Codex P2)

Batch asymmetry (documented for review): single create/PATCH 422 an explicit TOD flag on a
non-probate config; a BATCH spans multiple types, so the flag applies only to probate
children and is ignored (no 422) for the rest.

### Phase 4 — frontend (SEPARATE repo `bridgeleads-web`) — HELD until backend merges (user decision 2026-06-23).
Gated by the backend-first rule: regen `npm run gen:api-types` only after #117 merges to main + deploys,
so the generated contract carries `include_living_owner_tod`. Then build wizard checkbox + existing-user
notice. FE must NOT echo default `false` for a null config (null = grandfathered; only a real toggle writes).

## SHIPPED THIS SESSION
- **PR #117 OPEN** https://github.com/Abenezer1244/web-scrapper-automation/pull/117 — stacked on #115
  (base `feat/probate-tod-signal`, auto-retargets to main when #115 merges). 6 commits, Codex-clean.
- Migration 071 graph validated: single head, linear `071→070`, no collisions.
- ⏭️ USER/OPS: (1) merge #115 then #117; (2) run **migration 071 BEFORE** worker+api deploy;
  (3) Phase 4 FE after merge+regen.

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
