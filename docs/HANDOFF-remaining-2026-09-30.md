# Handoff: AI-mode removal (Phase 2b next) + what is left (2026-09-30)

**Read this first.** Then read:
- the top `docs/BUILD_JOURNAL.md` entry, 2026-09-30, "The audit queue closed, the egress
  proxy on, and AI mode on its way out";
- `tasks/todo-remove-ai-mode.md` (on main): the full phase plan, shaped by two Codex consults.

Main at handoff: **`11069d6a`** (#404). This doc is on branch
`docs/journal-2026-09-30-remaining` (PR **#408**, docs only, NOT merged), worktree
`C:/Users/Windows/bl-wt-journal2`.

## 1. Goal

Owner decision 2026-09-30: **"completely remove the AI mode from the SaaS and system."**

- **What "AI mode" really is:**
  - `county_connectors.scraper_mode = 'ai'` means "resolve a recorder-platform TEMPLATE
    from `base_url`" (`registry._detect_template`). There is **no LLM**.
  - It runs **17 of the 30 active production connectors**: benton, chelan, clallam,
    columbia, cowlitz, douglas, grant, island, jefferson, kitsap, lewis, okanogan, pacific,
    skagit, spokane, thurston, whitman. **Those counties must keep working.** The work is to
    rename the mode and remove every "AI" surface, not to delete scrapers.
- **Owner decisions** (2026-09-30):
  - rename the stored value to `'template'`;
  - lift the AI run cap;
  - remove Anthropic from the privacy/terms pages (merge only with counsel sign-off
    recorded);
  - S4-07: leave as is.

## 2. Current state

| Phase | What | State |
|---|---|---|
| 1 | Remove the monthly `ai_limit` run cap: BE #401 `17174adf` + FE bridgeleads-web#170 `58e9542` | **LIVE** |
| 2a | Readers accept `'template'` and `'ai'` (`registry.is_template_mode`, `TEMPLATE_MODES`); writers still store `'ai'` (#403 `351f0cf1`) | **LIVE** on api/worker/beat |
| 3 | Deleted `src/scrapers/ai/`, `enrichment/ai_assessor.py`, parcel fallback 3, the `anthropic` dep, settings `ANTHROPIC_API_KEY`/`AI_MODEL`/`AI_MAX_TOKENS`/`AI_SCRAPER_ENABLED`/`AI_COST_ALERT_THRESHOLD`/`AI_ENRICHMENT_ENABLED`. The PACS fallback URLs moved to `src/scrapers/enrichment/assessor_urls.py` (`KNOWN_ASSESSOR_URLS`) (#404 `11069d6a`) | **LIVE**, clean boot |
| **2b** | **Writers store `'template'` + migration moves the rows** | **NEXT: start here** |
| 4 | FE admin connectors page: no AI copy, sends `"template"` | before 2c |
| 4L | Privacy/terms: remove Anthropic | hard gate: counsel OK |
| 2c | Retire `'ai'`: straggler UPDATE + `CHECK (scraper_mode IN ('template','manual'))`, drop the alias | after 4 is live AND 7 days of zero `'ai'` |
| 5 | `docs/product/*`, `.agents/product-marketing-context.md`, registry docstrings | last |

Also shipped this session (all live):
- **#397, 5b-ii:** PACS, AcclaimWeb-PACS and Tracerfy use `pinned_session()`.
- **#399, 4b-ii:** the held-lead customer log line.
- **#407:** PyJWT 2.15.0.
- **D5-03:** `SCRAPER_EGRESS_PROXY_ENABLED=true` on the Railway worker.

The audit #4 and #5 code queues are **done**.

## 3. Phase 2b: exactly what to do (new branch off the then-current `origin/main`)

Suggested branch: `chore/remove-ai-mode-2b`, worktree `C:/Users/Windows/bl-wt-noai2b`.

**Writers today** (on `11069d6a`):
- `src/db/models.py:1188`: `scraper_mode = Column(String(16), nullable=False, default="ai")  # ai | manual`
- `src/api/schemas.py:2061`: `ConnectorCreate.scraper_mode: str = Field(default="ai", max_length=16)`;
  `:2091` is `ConnectorResponse.scraper_mode: str`.
- `src/api/routes/scrapers.py:1066`: `if body.scraper_mode != "ai":` returns 400 (the admin
  POST /scrapers/connectors can only create template connectors). `:1098` stores
  `body.scraper_mode`.

**2b changes:**
1. **Writers:** the model default becomes `'template'`. `ConnectorCreate` defaults to
   `'template'` and **normalizes an incoming `'ai'` to `'template'`** (a validator; `'ai'` is
   accepted but not advertised in OpenAPI). The route checks
   `registry.is_template_mode(body.scraper_mode)` and stores `'template'`.
   **Log every `'ai'` input** (one INFO line); 2c needs 7 days of zero.
2. **Migration 108** (107 is the last on main; **re-check the head before numbering**,
   because another session also adds migrations):
   - count rows by mode (log them);
   - `UPDATE county_connectors SET scraper_mode='template' WHERE scraper_mode='ai'`;
   - `ALTER ... SET DEFAULT 'template'`;
   - assert 0 `'ai'` rows and no unknown mode;
   - one short transaction.

   Downgrade: none needed. Rollback = redeploy the 2a code, which reads both values.
   Use `scripts/migrate.py` conventions (advisory lock) and check the alembic env.
3. **OpenAPI:** regenerate with `C:/Users/Windows/bl-rescat-venv` (CI-equivalent;
   `.venv-schema` is BROKEN). The diff should be only the ConnectorCreate default/description.
4. **Tests:**
   - creating a connector with no mode, or with `'ai'`, stores `'template'`;
   - the migration moves ai -> template and leaves manual alone;
   - all 17 templates still resolve after the migration (`get_scraper_class` for an
     `eagleweb.` / `tylerhost.net` / `/acclaimweb` style `base_url`).

   Prove each fails on main. **Import new helpers inside the tests that need them**, or the
   main-run fails on the import and proves nothing.
5. **After deploy, verify by the objects (prod, read-only):**
   `SELECT scraper_mode, count(*) FROM county_connectors GROUP BY 1` gives
   template=17 (+ any new), manual=13, ai=0. The worker logs show no
   `UnsupportedCountyError`.
6. **Process (every phase):**
   - Codex consult on the phase first (`codex exec -s read-only`, inline code, from the
     scratchpad; open the prompt with "Do NOT load any skill, do NOT run /graphify ...").
   - Then the fix and the related suites.
   - Then a Codex diff review until GATE: PASS. Never `codex review` (it runs pytest on your
     test DB).

## 4. Test environment

- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe` (PyJWT 2.13.0 there; prod is
  2.15.0; harmless).
- Env file: `source C:/Users/Windows/AppData/Local/Temp/claude/C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation/2f8f82a0-a92f-4ee3-81f1-6db58a9bf458/scratchpad/s402/testenv-5bii.sh`.
  - It points at DB `bridgeleads_5bii_test` and Redis **13**.
  - If the scratchpad is gone, copy `C:/Users/Windows/bl-wt-secaudit5/.unlazy/secaudit5/testenv.sh`
    and change the DB name and Redis index. Create the DB from a connection to
    `bridgeleads_secaudit5_test` (there is no `postgres` DB).
  - Redis 11/12 hold another rig's keys.
- Then `cd <worktree> && $PY -m alembic upgrade head` and
  `$PY -m pytest <files> -q -p no:cacheprovider -o addopts=""`. **Never bare pytest.**
- Suites for this area: `test_template_mode_read_both`, `test_config_eligibility`,
  `test_doc_type_select_wiring`, `test_collection_scope`, `test_scrapers`,
  `test_run_in_flight_guard`, `test_no_llm_code`, `test_no_ai_run_cap`, `test_workers`,
  `test_import_cycles`, `test_break_glass_login` (POSTs /scrapers/connectors).
- `test_auth.py::test_brute_force_lockout_after_five_failures` can fail in a full run
  (401 vs 429; shared lockout/Redis state). It passes alone.
- Lint: ruff 0.15.6, `ruff check src/ tests/`. No type-checker is configured.

## 5. Merging (merge = deploy)

1. **Another Claude session also merges to main** (the contact-lookup work, "1b-2").
   Protocol, agreed:
   - message it before every merge, and after each one lands;
   - the first green PR merges; the other rebases;
   - its address shows up in `ListAgents` / incoming cross-session messages.
2. `git fetch`; the PR must be CLEAN with `Test` + `Dependency Audit` green. Strict
   protection: BEHIND means merge `origin/main` into the branch and re-run CI (~20 min).
3. The quiet check is its **own call**, from the Railway-linked main checkout
   (`C:/Users/Windows/OneDrive - Seattle Colleges/Desktop/web-scrapper-automation`):
   `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe C:/Users/Windows/bl-checks/quiet.py`.
   Merge only on exit 0 with **all four counts present and 0**. A Railway "error decoding
   response body" means no counts: re-run it.
4. `gh pr merge <n> --merge --match-head-commit <sha>`. Never `--admin`. The auto-mode
   classifier may block a merge: then ask the owner, and don't work around it.
5. Verify: `railway deployment list --service {api,worker,beat} --json` shows SUCCESS on the
   merge commit; `/health` returns 200; the worker logs show `ready.` and no errors.
6. Poll CI in the **foreground** in blocks under 10 min. Background pollers time out or get
   reaped.

## 6. Failed attempts and dead ends (do not repeat)

- **A two-step rename (code writes `'template'` + migrate) is unsafe** while api, worker and
  beat restart at different moments. Codex consult r1/r2 FAILED it, hence the 3 steps.
- **A conflict resolution can break code silently.** #390 retyped the attempt token, and my
  ack bound it as a bare `started_at` inside a swallowing `except`. Only a real-path test
  caught it. After any merge, grep every consumer of a retyped value.
- **Tests that stub the pre-check with a literal IP prove nothing about DNS rebinding.**
  Model the rebinding resolver: public while the real validator runs, loopback afterwards.
- **`.venv-schema` is broken** (anaconda is gone). Don't use it.
- **`.env.example`** is blocked for Claude by a permission rule. Don't read or edit it.
- **PyJWT CVEs land often.** The Dependency Audit then fails every PR. Bump the minimal
  fixed version, and verify tokens minted by the old version plus malformed tokens.
- **Wait for #402-style migrations from the other session:** check `alembic/versions` for the
  real head before numbering.

## 7. Owner items (not Claude's)

1. **On/after 2026-10-01:** delete `ANTHROPIC_API_KEY`, `AI_ENRICHMENT_ENABLED` and
   `AI_SCRAPER_ENABLED` from Railway.
2. `.env.example`:
   - remove the `ANTHROPIC_API_KEY`/`AI_*` lines;
   - add `SCRAPER_EGRESS_PROXY_ENABLED=false` and `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE=25`.
3. Counsel sign-off for Phase 4L.
4. `pacs.co.douglas.wa.us` (`AcclaimWebScraper._PACS_URLS["douglas"]`) does not resolve in
   DNS. It needs Douglas County's current PACS URL.
5. Watch the scheduled scrapes with the proxy on: grep the worker logs for
   `egress proxy refused`. The only expected hit is `mtalk.google.com:5228`.
6. Older items: S3-16 (Tracerfy header), S3-04, S3-17, the DB stall on 2026-09-29.

## 8. Worktrees and leftovers (removing folders needs the owner's OK; never delete branches)

Merged: `bl-wt-5bii`, `bl-wt-4bii`, `bl-wt-noai`, `bl-wt-noai2a`, `bl-wt-noai3`,
`bl-wt-pyjwt215`, FE `blw-noai`. Open: `bl-wt-journal2` (#408).
Temp venvs `bl-venv-pyjwt214`/`bl-venv-pyjwt215`; DB `bridgeleads_5bii_test`.

## 9. Next step

1. `git fetch origin`. Read this file, the journal entry and `tasks/todo-remove-ai-mode.md`.
2. Ask the owner whether to merge #408 (docs only), with the cross-session courtesy message.
3. Start **Phase 2b** (§3) on a new branch off the current `origin/main`:
   - Codex consult;
   - tests that fail on main;
   - the fix;
   - suites;
   - Codex review to GATE: PASS;
   - PR, CI, coordinate with the other session, quiet gate, merge;
   - verify the 17/13/0 counts in prod.
4. Then Phase 4 (FE copy), then 4L when counsel approves, then 2c after 7 days, then 5.
