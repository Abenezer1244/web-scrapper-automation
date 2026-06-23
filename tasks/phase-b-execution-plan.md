# Phase B — Execution Plan (doc-type SELECT for all healthy pre_foreclosure counties)

> Worktree `.claude/worktrees/doctype-select-allcounty`, branch `feat/doctype-select-allcounty`
> (stacked on PR #114 `feat/doc-type-visibility` until it merges, then rebase onto main).
> Brief: `tasks/phase-b-doctype-select.md`. Design pressure-tested with Codex (consult 2026-06-23).

## Authoritative scope (live `GET /scrapers/connectors`, 2026-06-23)

22 pre_foreclosure connectors. King/Pierce already SELECT. Snohomish = single-type (NTS), leave as-is.
**Enable 15 HEALTHY counties. Defer 4 `health=down` (chelan, lewis, pacific, spokane) fail-closed (follow-up).**

| Family | Filter | confidence | Healthy counties to enable |
|---|---|---|---|
| Clark (clark_wa.py, manual) | server checkbox CODES | `verified` | clark |
| Skagit (templates/skagit_recording.py) | server dropdown + client refine (TWO stages) | `verified` | skagit |
| Whatcom (whatcom_wa.py, manual) | client keyword | `keyword` | whatcom |
| EagleWeb (templates/eagleweb.py) | client keyword | `keyword` | benton, clallam, grant, island, jefferson, kitsap, thurston, whitman |
| AcclaimWeb (templates/acclaimweb.py) | client keyword | `keyword` | douglas |
| iDocMarket (templates/idocmarket.py) | client keyword | `keyword` | columbia |
| Laserfiche (templates/laserfiche_weblink.py) | client keyword | `keyword` | cowlitz |
| Tyler SelfService (templates/tyler_selfservice.py) | client keyword | `keyword` | okanogan |

Note: okanogan = Tyler (NOT EagleWeb as brief guessed); grant = EagleWeb (its `/grantrecorder/web/` path wins over tylerhost domain).

## Codex findings folded into design (consult 2026-06-23)

- **C / ordering:** never flip `supported_for_selection=True` before the scraper `__init__` accepts `doc_types`. Guard test enforces it.
- **H / fail-closed:** explicit selection that maps to `[]` (unmappable/stale) → RAISE (job fails loud), never broaden to full. `None` = legacy/full only. Applies to King/Pierce too.
- **H / honesty:** surface `method`+`confidence` in API so keyword counties aren't visually identical to server-side `verified`. FE "best-effort text match" label = follow-up.
- **H / available = verified portal vocabulary**, NOT recent-histogram intersection (rare-but-real types stay selectable). Histogram is supporting evidence only.
- **H / Skagit narrows BOTH stages** (server dropdown iteration AND client refine keywords) or they contradict the checkbox.
- **M / per-county token maps** (built from shared template constants as defaults, not aliased to live shared lists).
- **M / `is_cancellation_or_admin` = lead-stage selection, not literal document selection** — document in registry notes.
- **L / audit:** include selected `doc_types` in job logs.
- **L / SHOW drift:** collection_scope (SHOW) still shows full family — separate follow-up.

## Phases (≤5 files each; live-verify + Codex review per phase)

- [x] **P0 — registry hardening, NO county flips (zero behavior change). DONE + Codex-reviewed (commits 71cd826/959efcb/61211fb).**
  - `doc_types.py`: add `canonical_tokens_or_raise(county,state,doc_types)` (raises on unmappable explicit selection); keep `canonical_tokens_for` for None/legacy paths.
  - King + Pierce: migrate to fail-closed helper.
  - API: add `pre_foreclosure_doc_type_method` + `_confidence` (additive, non-breaking) to ConnectorResponse + populate in `list_connectors`.
  - Guard test: every `supported_for_selection=True` county resolves to a scraper whose `__init__` accepts `doc_types`.
  - OpenAPI regen for the additive fields.
- [x] **P1 — Clark** DONE + Codex-reviewed (commit 3c2c87d). All 5 canonical types, narrows both checkbox codes + client label allowlist. Verified vs live prod (active row = clark_wa.ClarkWAScraper). ⚠️ NOTE: prod has a DEAD inactive duplicate clark row (state 'wa' lc, ai-mode, king_wa_probate.ClarkWaProbateScraper, probate-only) — ignore it; list_connectors serves only the active 'WA' row.
- [ ] **P2 — Skagit** (two-stage): wire both stages; `_AVAILABILITY` `verified`; live-verify dropdown; test; Codex.
- [ ] **P3 — EagleWeb** (8 counties): wire `__init__`; per-county `keyword` entries; live histogram spot-check 2-3 (e.g. thurston/kitsap/grant); test; Codex.
- [ ] **P4 — Acclaim/iDoc/Laserfiche/Tyler/Whatcom** (1 county each): wire each; per-county `keyword` entries; spot-check; test; Codex.
- [ ] **P5 — finalize:** full test pass, OpenAPI, BUILD_JOURNAL entry, PR. Track follow-ups: defer-4 counties, FE honesty label, SHOW-drift.

## Hard constraints
- FAIL-CLOSED: never flip a county to selectable without live verification of its mechanism + vocabulary.
- Git: additive push only, never delete/force-move branches (shared OneDrive repo).
- Tests: synthetic env only (conftest wipes tables under prod .env).
- Codex review after every phase; any Critical/High = NO-GO until fixed.

## Review (filled at end)
_TBD_
