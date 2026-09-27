# Security audit #3, Phase 2 (fixes)

Report: `SECURITY-AUDIT.md`. Phase 1 approved by the owner on 2026-09-27 ("go" for 2a). One PR per phase, at most 5
files. Each fix gets a regression test that fails on the pre-fix code, then a Codex diff review. Nothing merges
without the owner, because merging to main deploys.

## Phase 2a: S3-07 download revocation, S3-09 missing rate limits
Branch `fix/security-audit2-remaining` (rebased onto main `6194d73c`).

- [x] S3-07: `GET /jobs/{id}/download` had its own token-checking code, and it skipped the session-family
      revocation that `get_auth_context` enforces. Fixed at the root cause: the header path now goes through
      `get_auth_context`, the one decode point. `?token=` accepts ONLY `purpose=download` tokens; a full session
      JWT in the URL gets 401.
- [x] S3-09: per-user limits:
      - new zone `export` (20/min), shared by `/download` and `/export-url`;
      - new zone `writes` (30/min) for job cancel and scraper create, edit, csv-layout, and delete;
      - finished-job log replay gets the `general` zone per user.
- [x] Regression tests (real app in-process, isolated `_test` DB):
      - logout, then download with the old access token gives 401, via header and via query;
      - a session JWT in `?token=` gives 401;
      - a download token still gives 200 (control);
      - over-budget download, export-url, cancel, and scraper writes give 429, and the first N give 2xx.
- [x] Codex consult on this plan, and a Codex review of the diff.
- [x] Full suite on a fresh DB, ruff, OpenAPI check.

Files: `src/api/routes/jobs.py`, `src/api/routes/scrapers.py`, `src/api/middleware/rate_limit.py`,
`tests/test_audit3_download_and_limits.py`.

### Review (2a)
- Codex consult on the plan: GATE FAIL with 3 P1s, reconciled.
  - "?token= revocation must fail closed": it already did (503); the new tests cover both paths.
  - "fail-open limits on expensive routes": adopted. `export` and `writes` joined the Redis-outage fallback limiter.
  - "48 h emailed links": out of scope here. It is S3-30 (P3) in `SECURITY-AUDIT.md`, and the report takes
    precedence over Codex where it speaks.
- Tests: the 10 new tests failed 7/10 on the pre-fix code, each for the intended reason (200 where 401 was
  expected, no 429); the 3 that passed are the controls. All 10 pass after the fix.
- Test runs: 550 related tests passed. Full suite, fresh DB, 8 batches: 4,793 passed, 0 failed, 66 skipped.
  ruff is clean, and `export_openapi.py --check` is OK: the public docstring was kept, so the schema does not
  change and the frontend types are untouched.
- Codex diff review: GATE PASS, no findings.
- The frontend already downloads by Authorization header (`lib/api.ts` bearerFetch), and nothing sends a session
  JWT in `?token=`.
