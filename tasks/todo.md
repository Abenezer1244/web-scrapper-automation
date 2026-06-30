# Test-DB prod-safety guard (root-cause fix for 2026-06-29 prod wipe)

## Incident (confirmed against prod, read-only)
- prod `users` = 85 rows, INTACT, original `created_at` (admin 2026-03-24).
- Physically wiped (pg_stat live tuples ≈ 0): `results`, `job_logs`,
  `property_list_membership`, `jobs`(1), `scraper_configs`(1) — EXACTLY the 5
  tables the `db` fixture teardown deletes, all sharing a synchronized
  autovacuum at **2026-06-29 23:49 UTC** (Postgres after a mass delete).
- `results.n_tup_del` = 13.8M cumulative DELETEs (ORM `delete()`, not TRUNCATE).
- Tables conftest does NOT touch (`county_records`, `notifications`,
  `batch_runs`, `scraper_batches`) retain live rows → not a global truncate/restore.
- Root cause: `tests/conftest.py` `db`-fixture teardown runs UNCONDITIONAL
  `delete(JobLog/Result/PropertyListMembership/Job/ScraperConfig)` with no prod
  guard; tests read `settings.DATABASE_URL` (shared/synced `.env`); a `pytest`
  run with `DATABASE_URL`→prod wiped tenant data. (Codex consult agreed.)

## Approach (user choice: layered + separate TEST_DATABASE_URL)
Make it impossible for the suite to acquire a destructive connection to a
non-test DB — don't just make teardown "more careful".

## Tasks
- [x] Consult Codex on root-cause ranking + prevention design.
- [x] `tests/_db_safety.py`: `enforce_test_database()` (require TEST_DATABASE_URL,
      validate name `_test`/`_testing` + local/allowlisted host, pin
      DATABASE_URL/_SYNC + ENVIRONMENT, hard-abort otherwise) and
      `assert_engine_is_test()` belt.
- [x] `tests/conftest.py`: call guard at top BEFORE any `src.*`/`main` import;
      add `pytest_configure` belt; assert in `db` teardown right before DELETEs;
      drop now-dead `import os`.
- [x] `.github/workflows/ci-cd.yml`: add `TEST_DATABASE_URL(_SYNC)`.
- [x] `.claude/rules/testing.md`: document the mandatory guarded test DB.
- [x] Verify: 8/8 guard unit cases pass; conftest import hard-aborts on
      missing + prod-like TEST_DATABASE_URL (exit 1) before any DB connect;
      `py_compile` clean.
- [x] Codex review of the diff (gate). 4 passes. No Critical/High at any point.
      3 P2s found + fixed: (1) reject DSN query host/db overrides; (2) reject
      hostless DSNs (PGHOST bypass); (3) require explicit TEST_DATABASE_URL_SYNC
      (the :5432->:6543 rewrite breaks a derived sync URL). Final review: CLEAN.
- [ ] `.env.example`: add TEST_DATABASE_URL(_SYNC) — BLOCKED: harness denies
      all `.env*` access. **User to add manually** (see testing.md for values).

## Out of scope (separate follow-ups)
- DATA RECOVERY: restore prod from a Supabase backup taken before
  2026-06-29 23:49 UTC. User/ops action (Supabase dashboard).
- DB-layer least-privilege (test role can't DELETE prod) — optional hardening.
- Teardown row-scoping to test-owned rows: deferred — current unconditional
  deletes preserve cross-test isolation (register tests create non-test-domain
  users); the guard makes prod-scope impossible, so scoping is lower priority.
  Flagged for Codex.
- Trial→"Pro" display: by-design 7-day trial; verify `expire_trials` runs in
  prod + UI shows "Trial · N days". Separate ticket.

## Review
- Guard runs at conftest import before `from main import app` / settings, so the
  SystemExit abort precedes any engine construction or connection. Proven via
  `python -c "import tests.conftest"` with no/prod TEST_DATABASE_URL → abort.
- CI behavior unchanged: TEST_DATABASE_URL = existing `bridgeleads_test`@localhost,
  guard pins DATABASE_URL to the same value.
- No secrets added; no app/runtime code touched (tests + CI + docs only).
