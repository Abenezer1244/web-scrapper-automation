---
description: Rules for writing and modifying tests
globs: tests/**/*.py
---

# Testing Rules

- THIS IS A REAL PRODUCTION PROJECT — never use mocks, stubs, or dummy data
- Tests must use real settings and real file I/O where applicable
- Use `pytest` conventions: `test_` prefix for all test functions and files
- Do not use `unittest.mock` or `pytest-mock` unless absolutely required by an external API rate limit
- Test files mirror the `src/` structure (e.g., `tests/test_data_exporter.py` tests `src/utils/data_exporter.py`)
- Always clean up created files in tests (use `tmp_path` fixture or teardown)
- Never assert on hardcoded timestamps — use existence checks or relative comparisons
