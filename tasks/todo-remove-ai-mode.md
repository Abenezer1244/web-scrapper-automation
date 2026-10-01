# Remove "AI mode" from BridgeLeads (owner decision 2026-09-30)

Owner: "I want to completely remove the AI mode from the SaaS and system."
Branch `chore/remove-ai-mode` (this plan only). Each phase gets its own branch/PR.

## What "AI mode" actually is today (verified on origin/main `f4effe7b` + prod, 2026-09-30)

- **It is not an LLM.** A connector with `scraper_mode = 'ai'` picks one of 8 hand-written
  recorder-platform templates by matching its `base_url` (`src/scrapers/registry.py`
  `_detect_template`). The old Claude-driven `AIScraper` was removed long ago.
- **It runs 17 of the 30 active production connectors** (benton, chelan, clallam, columbia,
  cowlitz, douglas, grant, island, jefferson, kitsap, lewis, okanogan, pacific, skagit,
  spokane, thurston, whitman). Removing the *mode* must NOT remove these counties: the
  templates are the scrapers.
- **The only real LLM code** is `src/scrapers/ai/` (Claude client, navigator, extractor,
  paginator, cache) and `src/scrapers/enrichment/ai_assessor.py` (a parcel-enrichment
  fallback). In production `AI_ENRICHMENT_ENABLED=false`, so **nothing calls Claude today**.
  `ANTHROPIC_API_KEY` is set on the worker but unused. `anthropic==1.3.0` is in requirements.
- **Product surfaces:**
  - the `ai_limit` run-refusal code;
  - `AI_JOB_LIMITS` (Starter 5 / Pro 50 / Business 500 per month, counted on ai-mode
    connectors), enforced in `src/api/config_eligibility.py` (POST /jobs and GET /scrapers);
  - `RUN_REFUSAL_CODES` and `RunEligibility` in `schemas.py` / `errors.py`, and so in
    `schema/openapi.json`;
  - the admin "Add AI-powered county" page, which can only create ai-mode connectors;
  - the privacy/terms pages, which list Anthropic as a subprocessor;
  - product docs and marketing context.

## Status (2026-10-01)

- [x] Phase 1: AI run cap removed (#401, bridgeleads-web#170). LIVE.
- [x] Phase 2a: readers accept 'template' and 'ai' (#403). LIVE.
- [x] Phase 3: LLM code, `anthropic` and the `AI_*` settings deleted (#404). LIVE.
- [x] Phase 4: admin connectors page (bridgeleads-web#171, `7bf55cca`). LIVE. Shipped BEFORE 2b:
      the old badge compared `=== "ai"` and would have labelled all 17 template counties "Manual".
- [x] Phase 2b: writers store 'template', `'ai'` input normalized + logged, migration **108**
      (#409, `d65a3b09`). LIVE. Prod: active template=17, manual=13, ai=0 (21 inactive rows
      moved too), default 'template', 30/30 active connectors resolve.
- [x] Phase 5: product docs, marketing context, source comments (this PR).
- [ ] Phase 4L: legal pages. Draft PR open; merges ONLY with recorded counsel sign-off.
- [ ] Phase 2c: NOT before 2026-10-08. Gate (tightened by Codex consult 2b r1, P1): zero
      `'ai'` ROWS in `county_connectors` checked daily for 7 days, zero
      `connector_create: legacy scraper_mode 'ai' normalized` log lines, AND no API instance
      older than #409 alive. A log alone misses an old writer.
- 🛑 Rollback after 108 is NOT "redeploy the 2a image": 2a has no 108 file, so its API boot
  refuses to start. Roll back with a revert PR that KEEPS `alembic/versions/108_*.py`.
- Two comments remain in files another session is editing (`src/workers/__init__.py:142`,
  `src/workers/tasks_helpers/enrich.py:156`); fold them into 2c.

## Target end state

- No "AI" anywhere a customer, admin or reader sees it. The template path is simply "template".
- No per-plan AI run cap. The record quota and the entitlement rules still govern runs.
- No LLM code and no `anthropic` dependency. Anthropic is off the subprocessor list.
- All 30 connectors keep working with no change in what they scrape.

## Phases (revised after Codex consult r1 GATE FAIL, 2026-09-30)

Rules for every phase:
- A NEW branch off the then-current `origin/main` (never off an earlier phase's branch).
- Codex consult first, then a regression test proven to fail on main, then the fix, the
  related suites and a Codex review.
- After merge: CI green, the quiet gate, the merge, and a deploy check on api, worker AND
  beat (all three SUCCESS on the commit) before the next phase starts.

### Phase 0: acceptance gates (no code)
- Record, from prod: connectors by mode (17 ai / 13 manual today); ai-mode connectors
  created since; `ANTHROPIC_*`/`AI_*` variables per service.
- Grep for every reference to the mode, AI copy, `anthropic` and `src.scrapers.ai`,
  including scripts, fixtures, CI config and the FE repo.
- Define a canary for all 30 connectors: resolve the scraper class for each, and for the
  17 template ones run the existing canary path. Use it before and after Phases 2c and 3.

### Phase 1: remove the AI run cap (BE, then FE types)
- Remove ONLY the `ai_limit` value and its behaviour:
  - the `ai_limit` code, `ai_limit_message`, the AI count and the ai-connector lookup in
    `config_eligibility.py`;
  - `AI_JOB_LIMITS`;
  - `"ai_limit"` from `RUN_REFUSAL_CODES` and the two Literals.
- `resumes_at` keeps serving `over_limit`, and every other refusal code is unchanged.
- Regenerate `schema/openapi.json` (`.venv-schema`, vs origin/main).
- Then an FE PR: regenerate types, drop the `ai_limit` handling and copy.
- Tests: the cap no longer refuses (fails on main). Every other refusal code (over_limit,
  frozen, ended, run_in_flight, config_inactive, not_entitled) still refuses as before,
  via POST /jobs and GET /scrapers.

### Phase 2: rename the mode `'ai'` -> `'template'`, in three deploys
- **2a: read-both release.** Every reader (registry `get_scraper_class`,
  `connector_scraper_class`, `list_supported`, canary, daily_scrape) treats `'template'`
  exactly like `'ai'`. **Writers still write `'ai'`**: model default, `ConnectorCreate`
  default, POST /scrapers/connectors. No data change. Rollback-safe: old code never sees
  `'template'`.
- **2b: switch writes + migrate.** Only after 2a is SUCCESS on api, worker and beat.
  - Writers produce `'template'`, and incoming `'ai'` is normalized to `'template'` on
    write (an input alias, not advertised in OpenAPI).
  - Migration 108 (107 was taken): count by mode, then `UPDATE ... SET scraper_mode='template' WHERE
    scraper_mode='ai'`, server default `'template'`. Assert 0 `'ai'` rows and no unknown
    mode. One short transaction.
  - Rollback = redeploy 2a code, which reads both. No down-migration is needed.
  - Run the 30-connector canary after deploy.
  - OpenAPI regenerated in 2b: advertises `'template'` (and `'manual'`); `'ai'` is accepted
    but not advertised.
- **2c: retire `'ai'`**, only after Phase 4 (the admin page no longer sends `'ai'`) is live,
  and after 7 days with zero `'ai'` inputs logged by 2b and zero `'ai'` rows.
  - Migration 109+ re-runs the idempotent UPDATE, which catches a straggler written by an old
    API instance during 2b's rolling deploy.
  - It then adds `CHECK (scraper_mode IN ('template','manual'))`, so no `'ai'` or unknown mode
    can be stored again.
  - Drop the reader and input alias, regenerate OpenAPI, and rewrite the "Claude AI"
    docstrings.
  - Rollback: redeploy 2b code. It reads both values, and the constraint only forbids writing
    `'ai'`, which 2b never does.

### Phase 3: delete the LLM code (BE)
- Preconditions:
  - Phase 2 is done;
  - no queued or in-flight job references the deleted modules (Celery tasks never import
    `src.scrapers.ai`; confirm by grep and by a quiet queue);
  - `AI_ENRICHMENT_ENABLED` is still false in prod.
- Move `_KNOWN_ASSESSOR_URLS` to a plain module. Delete `src/scrapers/ai/`, `ai_assessor.py`
  and parcel fallback 3. Drop the `AI_*`/`ANTHROPIC_*` settings and `anthropic` from
  requirements (pip-audit §12). Verify the built image no longer installs it.
- Owner deletes the Railway variables only after the rollback window (one day), so a
  rollback to the previous image still boots.

### Phase 4: frontend copy (FE); ships BEFORE 2c
- The admin connectors page ("Add county", no AI badge, sends `"template"`) and `lib/types.ts`.
- A grep gate: no "AI", "Claude" or "Anthropic" in `app/`, `components/` or `lib/`,
  except the legal pages below.

### Phase 4L: legal pages (hard gate)
- Remove Anthropic from `privacy` and `terms` ONLY with owner/counsel approval recorded
  in the PR. Without it, the PR stays open, and nothing else depends on it.

### Phase 5: docs
- `docs/product/*`, `.agents/product-marketing-context.md`, the registry docstrings.
- Grep gate: no remaining "AI mode", "AI-powered" or "Claude" outside history
  (BUILD_JOURNAL, tasks/, old audits).

## Codex consult record
- **r1 GATE FAIL:** mixed-version rename, rollback, an over-broad Phase 1, the branch model,
  the legal gate. Adopted: read-both first, writers unchanged; Phase 1 removes only
  `ai_limit`; each phase branches from current main; legal is its own gated PR.
- **r2 GATE FAIL.** Adopted:
  - FE before alias retirement;
  - a straggler UPDATE plus a CHECK constraint in 2c;
  - OpenAPI regenerated in each of 2a/2b/2c;
  - a 7-day zero-`'ai'` window;
  - a Douglas DNS baseline exception in the canary.

  Carried into each phase's own consult rather than the plan:
  - rollback proof per phase;
  - queue drain detail for Phase 3 (no Celery task imports `src.scrapers.ai`; verify there);
  - image/SBOM checks;
  - behavioural checks for the 13 manual connectors.

  The legal gate is enforced by the owner's merge: Phase 4L is merged only with recorded
  counsel approval, and no other phase depends on it. Phase 5's grep gate excludes the
  legal pages until 4L lands.

## Decisions needed from the owner before Phase 1
1. Rename the stored value to `'template'` (Phase 2, two deploys), or keep the internal
   value `'ai'` and remove only what anyone can see (skip Phase 2)? Recommended: rename.
2. The legal pages change (Phase 4): OK to remove Anthropic from the subprocessor list
   once Phase 3 is live? It already receives no data in production.
3. Phase 1 lifts a cap: Starter/Pro/Business lose the "N runs a month on AI counties"
   limit. Run volume is then bounded only by the record quota and entitlements. OK?

## Known side findings (not part of this plan)
- `acclaimweb._PACS_URLS["douglas"] = https://pacs.co.douglas.wa.us/...` does not resolve
  in DNS (found by the D5-03 probe, 2026-09-30). Douglas address lookups can never succeed.
