---
description: Rules for writing and modifying tests
globs: tests/**/*.py
---

# Testing Rules

- **TEST DATABASE IS MANDATORY AND GUARDED.** The suite reads `TEST_DATABASE_URL`
  (NOT `DATABASE_URL`). `tests/_db_safety.py::enforce_test_database()` runs at the
  top of `conftest.py` before any `src.*` import: it validates `TEST_DATABASE_URL`
  (DB name must end with `_test`/`_testing`; host must be local or in
  `TEST_DB_HOST_ALLOWLIST`), pins `DATABASE_URL`/`DATABASE_URL_SYNC` to it, and
  forces `ENVIRONMENT=test`. If `TEST_DATABASE_URL` is missing/unsafe the run
  HARD-ABORTS. This exists because the `db` fixture teardown issues unconditional
  DELETEs — a `pytest` run against prod once wiped the production database. Set
  BOTH `TEST_DATABASE_URL` and `TEST_DATABASE_URL_SYNC` to a local/test DB
  (the sync URL is required, not derived — `src.db.session` rewrites
  `:5432/`→`:6543/` so a derived one would break sync tests); never point either
  at production. The guard also rejects hostless DSNs and DSNs that smuggle a
  `host`/`dbname` override in the query string. Never weaken or bypass this guard.
- THIS IS A REAL PRODUCTION PROJECT — never use mocks, stubs, or dummy data
- Tests must use real settings and real file I/O where applicable
- Use `pytest` conventions: `test_` prefix for all test functions and files
- Do not use `unittest.mock` or `pytest-mock` unless absolutely required by an external API rate limit
- Test files mirror the `src/` structure (e.g., `tests/test_data_exporter.py` tests `src/utils/data_exporter.py`)
- Always clean up created files in tests (use `tmp_path` fixture or teardown)
- Never assert on hardcoded timestamps — use existence checks or relative comparisons
