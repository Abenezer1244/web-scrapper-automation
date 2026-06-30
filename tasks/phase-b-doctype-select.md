# Phase B — Enable user-selectable pre-foreclosure document types for ALL counties

> Live-verified, county-by-county. This is the handoff brief for a fresh session.
> Follow the standing rules: consult Codex BEFORE building and have Codex review AFTER
> (`.claude/rules/codex-collaboration.md`); never guess/assume; fix from root cause;
> live-verify everything; cross-check with Codex.

## Goal

Today the wizard's "Document types to scrape" CHECKBOX selector (the **SELECT** feature)
appears ONLY for **King** and **Pierce** pre_foreclosure. Every other pre_foreclosure
county hides it (fail-closed). Turn the selector on for ALL pre_foreclosure counties —
but ONLY after live-verifying each county's real portal doc-type vocabulary (exactly how
Clark was verified in the prior session).

**Scope = pre_foreclosure record type only.** Generalizing SELECT to probate/tax/divorce
is a SEPARATE later effort.

## Key distinction — do not confuse these two features

- **SELECT (this task):** `src/scrapers/doc_types.py`. The capability that lets users PICK
  doc types. API field `pre_foreclosure_doc_types`. Renders as checkboxes in the wizard.
- **SHOW (already built; PRs OPEN):** read-only "Documents collected" panel in
  `src/scrapers/doc_scope.py`. Backend `web-scrapper-automation` **#114**
  (`feat/doc-type-visibility`), frontend `bridgeleads-web` **#50** (`feat/doc-type-show-ui`).
  DIFFERENT feature. **Branch Phase B off `main` AFTER #114 merges** — #114 added Clark's
  verified checkbox→label map (`_CHECKBOX_DOC_LABELS` in `clark_wa.py`) and the
  `connector_scraper_class` resolver; reuse them.

## How SELECT works (the mechanism to replicate per county)

In `src/scrapers/doc_types.py`:
- `_AVAILABILITY: dict[(county,state)] -> {available:[canonical_tokens], method, confidence,
  default, supported_for_selection: bool, tokens:{canonical_token -> scraper_token}, note}`.
- Canonical tokens: `notice_of_default, notice_of_trustee_sale, lis_pendens,
  notice_of_foreclosure, foreclosure`.
- Only `("king","wa")` and `("pierce","wa")` have `supported_for_selection=True`,
  `confidence="verified"`. `_EAGLEWEB_TEMPLATE` is `supported_for_selection=False`;
  `_EAGLEWEB_COUNTIES={"kitsap"}`.
- Helpers: `availability_for`, `validate_selection`, `selectable_doc_type_labels`,
  `canonical_tokens_for(county,state,doc_types) -> scraper's own tokens`.

**To enable a county, do THREE things (per county):**
1. **Live-verify** the portal: enumerate the exact doc-type options it exposes for
   pre_foreclosure and map each to a canonical token. Method differs by scraper family.
2. Add a real `_AVAILABILITY` entry (or extend the EagleWeb-template path) with verified
   `tokens`, `supported_for_selection=True`, `confidence="verified"`.
3. **Wire the scraper constructor** to accept `doc_types` and NARROW its filter when
   present, falling back to the full set when `None` (like King/Pierce already do). Today
   only King (`src/scrapers/king_wa_probate.py`, search_text method) and Pierce
   (`src/scrapers/pierce_wa_probate.py`, checkbox-id method) call `canonical_tokens_for(...)`
   in `__init__`. Every other scraper needs this added, mapped to ITS mechanism.

**Frontend needs NO change** — the wizard's `CountyStep` already renders checkboxes for ANY
county whose API response includes `pre_foreclosure_doc_types`. Verify this in
`bridgeleads-web/app/(dashboard)/scrapers/new/_steps/CountyStep.tsx`. So Phase B is
**backend-only** (+ live verification).

## Counties + selection mechanism (VERIFY each live; don't trust these notes)

Get the authoritative list from the public `GET /scrapers/connectors` (see dogfood recipe).
Pre_foreclosure-capable connectors and their families:

- **clark** (`clark_wa.py`): CHECKBOX, server-side filter. **Already live-verified** in the
  prior session — `_DOC_TYPE_CHECKBOX_VALUES["pre_foreclosure"]` + `_CHECKBOX_DOC_LABELS`:
  167=NOTICE OF TRUSTEE SALE, 129=LIS PENDENS, 166=NOTICE OF DEFAULT, 157=NOTICE OF
  FORECLOSURE, 93=FORECLOSURE, 257=TRUSTEES SALE. **Easiest — do first.**
- **skagit** (`templates/skagit_recording.py`): server dropdown `_SERVER_DOC_TYPES` has exact
  labels (Lis Pendens, Notice Of Default, Notice Of Foreclosure, Notice Of Trustees Sale)
  + client refinement.
- **EagleWeb group** (`templates/eagleweb.py` `_DOC_TYPE_MAP`): benton, clallam, grant,
  island, jefferson, kitsap, okanogan, thurston, whitman, etc. KEYWORD client-side filter.
- **whatcom** (`whatcom_wa.py` `_DOC_TYPE_KEYWORDS`): Helion KEYWORD filter.
- **chelan/douglas** (`templates/acclaimweb.py`): KEYWORD.
- **columbia** (`templates/idocmarket.py`): KEYWORD.
- **cowlitz** (`templates/laserfiche_weblink.py`): KEYWORD.
- **snohomish** (`snohomish_wa_pre_foreclosure.py`): newspaper, NTS-only — single type;
  selection trivial/N-A.

## How to live-verify (recon, like Clark)

Write throwaway Playwright recon scripts (Python; playwright installed) that open each
county portal and ENUMERATE the real doc-type options:
- **Checkbox/dropdown portals** (clark, skagit): dump every option id + visible label, map to
  canonical tokens. (Clark recon enumerated 348 modal checkboxes via
  `input[type=checkbox][id^='dt-DocumentType-']`.)
- **Keyword portals** (EagleWeb/whatcom/etc): client-side filtering — run a dated search and
  histogram the actual returned `doc_type` strings to confirm which canonical types really
  appear; "selection" then narrows the keyword list to that canonical subset.
- **Prior-session finding:** Clark's LandmarkWeb portal DOES filter server-side by the
  selected doc-type CODE (it does NOT "return everything"), so the checkbox/code list is
  load-bearing. Re-verify this per portal.

County portal `base_url`s are in the `county_connectors` DB table and in each scraper file.

## Local dogfood recipe (needed to run the real app locally)

The app encrypts `users.email` and needs the real `FIELD_ENCRYPTION_KEY` (Railway-injected);
`REDIS_URL` points at Railway's PRIVATE `redis.railway.internal` (unreachable locally) and
`/auth/me` fails closed without Redis. So:
1. Local empty Redis: `python -c "from fakeredis import TcpFakeServer; TcpFakeServer(('127.0.0.1',6380),server_type='redis').serve_forever()"` (`pip install "fakeredis>=2.21"`) — run in background.
2. Backend (real prod secrets + your branch code), from the backend worktree:
   `railway run sh -c 'REDIS_URL=redis://localhost:6380 ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000 DEBUG=true python -m uvicorn main:app --host 127.0.0.1 --port 8000 --log-level warning'`
   (railway CLI is authed to bridgeleads-production; `GET /scrapers/connectors` is PUBLIC.)
3. Frontend (only for UI QA): in the FE worktree `npm install` first (OneDrive node_modules
   is partial-synced, missing `@types/react`), set `.env.local`
   NEXT_PUBLIC_API_URL=http://localhost:8000 + NEXTAUTH_URL=http://localhost:3000
   (+ NEXTAUTH_SECRET from main checkout), then next dev :3000. Admin:
   admin@bridgeleads.io / BridgeLeads2026! (401s with the fallback key; works under `railway run`).
4. CLEAN UP after: kill servers, delete copied `.env`/`.env.local` (hold secrets; gitignored
   but remove). Zero writes to prod (login + GET only).

## Hard constraints

- REAL production SaaS. No mocks/dummy/test code in the codebase. Multi-tenant: every query
  filters by `user_id`.
- FAIL-CLOSED: never flip `supported_for_selection=True` for a county you haven't
  live-verified. `confidence` must be `"verified"`.
- Git hazard (shared OneDrive repo, concurrent sessions): work in an isolated `git worktree`,
  additive push only, NEVER delete/force-move branches, re-verify the branch tip before any
  branch op. (memory: `feedback_no_branch_delete_shared_onedrive`)
- Tests: `pytest` conftest WIPES tables under the prod `.env` — run unit tests ONLY under a
  synthetic env (throwaway SECRET_KEY/DATABASE_URL/DATABASE_URL_SYNC/REDIS_URL), or verify
  logic via direct imports. `ruff` lives at `anaconda3/Scripts`.
- OpenAPI: `schema/openapi.json` — a local regen drifts (FastAPI/Pydantic version-sensitive);
  hand-edit to the pinned convention, or regen only in the pinned `.venv-schema`. FE types
  come from `npm run gen:api-types` (pulls web-scrapper-automation/main/schema/openapi.json)
  AFTER backend merges.
- Codex: `codex` skill. `codex review --base <ref>` has been TIMING OUT (gpt-5.5); prefer
  streaming `codex exec` with the diff embedded, medium effort.

## Suggested order

1. Brainstorm + consult Codex on the per-county wiring design. KEY question: keyword-portal
   SELECT semantics — narrowing a *client-side* keyword filter is weaker than a *server-side*
   checkbox/dropdown. Decide how honest that is, and whether keyword counties should be
   `confidence:"keyword"` (not `"verified"`) or need extra validation.
2. **Clark first** (already verified) — wire `doc_types` into `ClarkWAScraper`, add the
   `_AVAILABILITY` entry, test.
3. Then server-filtered portals (skagit), then keyword counties after live histogram
   verification.
4. Snohomish: single-type — handle trivially or leave as-is.
5. Per county: live-verify -> add `_AVAILABILITY` -> wire scraper `doc_types` narrowing ->
   unit test -> Codex review.
6. Deliverable: one backend PR on its own worktree branch. Confirm the FE needs no change
   (test live that a newly-enabled county now shows checkboxes).

**Start by reading** `src/scrapers/doc_types.py`, `src/scrapers/king_wa_probate.py` +
`pierce_wa_probate.py` (the two working SELECT implementations), and
`src/api/routes/scrapers.py` `list_connectors`. Then brainstorm + consult Codex before code.
