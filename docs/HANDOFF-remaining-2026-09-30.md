# Handoff: what is left after 2026-09-30

Read first: the top BUILD_JOURNAL entry (2026-09-30, "The audit queue closed, the egress
proxy on, and AI mode on its way out") and `tasks/todo-remove-ai-mode.md`.

## 1. State

- **Security audits #4 and #5:** every code finding is fixed and live. Last this session:
  5b-ii #397, 4b-ii #399, and the D5-03 egress proxy turned ON in production.
- **S4-07:** the owner chose to leave it as is.
- **PyJWT 2.15.0** is live (#407).
- **AI-mode removal** (owner decision 2026-09-30):

| Phase | What | Status |
|---|---|---|
| 1 | remove the `ai_limit` run cap (BE #401 + FE bridgeleads-web#170) | LIVE |
| 2a | readers accept `'template'` and `'ai'` (#403) | LIVE on api/worker/beat |
| 3 | delete the LLM code, `anthropic`, the `AI_*` settings (#404) | OPEN, Codex PASS; merge after the other session's #406 |
| 2b | writers store `'template'`; migration (number **108 or later**: 107 is taken) moves the rows | next |
| 4 | FE admin connectors page: no AI copy; sends `"template"` | before 2c |
| 4L | privacy/terms: remove Anthropic (owner approved; merge only with counsel OK recorded) | |
| 2c | retire `'ai'`: straggler UPDATE + CHECK constraint | after 4 is live AND 7 days of zero `'ai'` |
| 5 | product docs | last |

## 2. Rules that bit this session

- **Another session merges to main.** Message it before every merge; the first green PR
  merges; the other rebases. Strict protection means each main move costs a ~20 min CI round.
- **Quiet check and merge are separate calls.** Merge only on exit 0 with all four counts
  present and 0. A Railway CLI "error decoding response body" gives no counts: re-run it.
- **Proving on main:** import new helpers inside the tests that need them, or the main-run
  fails on the import and proves nothing.
- **OpenAPI:** `.venv-schema` is broken (anaconda is gone). `C:/Users/Windows/bl-rescat-venv`
  reproduces main exactly (`export_openapi.py --check` OK on main); use it.
- **`.env.example`** is blocked for Claude by a permission rule: owner edits only.

## 3. Owner items

1. `.env.example`:
   - remove the `ANTHROPIC_API_KEY` and `AI_*` lines;
   - add `SCRAPER_EGRESS_PROXY_ENABLED=false` and `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE=25`.
2. One day after #404 deploys, delete `ANTHROPIC_API_KEY`, `AI_ENRICHMENT_ENABLED` and
   `AI_SCRAPER_ENABLED` from Railway.
3. Counsel sign-off for Phase 4L.
4. `pacs.co.douglas.wa.us` (`AcclaimWebScraper._PACS_URLS["douglas"]`) no longer resolves in
   DNS, so Douglas address lookups can never succeed. It needs the county's current PACS URL.
5. Unchanged from before:
   - S3-16 (Tracerfy webhook header);
   - Cloudflare sole ingress (S3-04) and admin password rotation (S3-17);
   - the unexplained DB stall on 2026-09-29.
6. Watch the first scheduled scrapes with the proxy on. Grep the worker logs for
   `egress proxy refused`; the only expected hit is `mtalk.google.com:5228`.
7. Upgrade PyJWT to 2.15.0 in the shared `bl-rescat-venv` when no session is running tests.

## 4. Worktrees made this session (merged ones can be removed with the owner's OK)

`bl-wt-5bii`, `bl-wt-4bii`, `bl-wt-noai` (#401), `bl-wt-noai2a` (#403),
`bl-wt-noai3` (#404, open), `bl-wt-pyjwt215` (#407), `bl-wt-journal2` (this doc), FE
`blw-noai` (#170). Also the temp venvs `bl-venv-pyjwt214` and `bl-venv-pyjwt215`, and the
DB `bridgeleads_5bii_test` (Redis 13).
