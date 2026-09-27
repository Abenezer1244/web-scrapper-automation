"""Test-database safety guard — ROOT-CAUSE FIX for the 2026-06-29 prod wipe.

The `db` fixture teardown in conftest.py issues UNCONDITIONAL DELETEs across
results / jobs / scraper_configs / job_logs / property_list_membership. Because
the suite connected via ``settings.DATABASE_URL`` (the same ``.env`` the app
uses) and this repo lives in a *synced* shared folder driven by several agents,
a ``pytest`` run whose ``DATABASE_URL`` pointed at the production Supabase DB
wiped every tenant's data while leaving the ``users`` rows intact.

This module makes it PHYSICALLY IMPOSSIBLE for the suite to touch a non-test
database. ``enforce_test_database()`` MUST be called at the very top of
``tests/conftest.py`` — BEFORE ``src.config.settings`` (and therefore the DB
engine) is imported — so the override lands before the settings singleton reads
the environment.

Policy (all enforced; any failure aborts collection BEFORE a fixture can run):
  * ``TEST_DATABASE_URL`` is REQUIRED. The suite never falls back to
    ``DATABASE_URL`` — that is exactly the variable a synced ``.env`` can point
    at prod.
  * ``TEST_DATABASE_URL`` must be a *recognisable* test database: its name ends
    with one of ``TEST_DB_NAME_SUFFIXES`` AND its host is local or explicitly
    allowlisted via ``TEST_DB_HOST_ALLOWLIST``.
  * The validated URLs OVERRIDE ``DATABASE_URL`` / ``DATABASE_URL_SYNC`` in the
    environment, so whatever the shared ``.env`` held is discarded for the run.
    ``DATABASE_URL_MIGRATE`` (the owner role Alembic prefers) is pinned to the
    test sync URL too: PINNED, never deleted, because an absent key is exactly
    what ``load_dotenv()`` and pydantic's ``env_file`` refill from a ``.env``.
  * No libpq variable that reroutes an explicit DSN (``PGHOSTADDR``, a service
    file) may be set.
  * ``ENVIRONMENT`` is forced to ``"test"``.

What counts as a test database lives in ``src/db_safety.py``, shared with
``alembic/env.py`` so the two can never disagree.
"""
from __future__ import annotations

import os

from src.db_safety import ambient_redirects
from src.db_safety import classify as _classify


def _abort(reason: str) -> None:
    """Abort the whole test process loudly. ``SystemExit`` (not a plain
    ``Exception``) so it is never swallowed by a stray ``except Exception``."""
    raise SystemExit(
        "\n"
        "==================== TEST DATABASE SAFETY ABORT ====================\n"
        f"{reason}\n\n"
        "The test suite refuses to run unless TEST_DATABASE_URL points at a\n"
        "dedicated test database (name ends with _test/_testing; host local or\n"
        "listed in TEST_DB_HOST_ALLOWLIST). This guard exists because an\n"
        "unguarded test teardown once wiped the PRODUCTION database.\n"
        "Set TEST_DATABASE_URL and TEST_DATABASE_URL_SYNC (both required, explicit\n"
        "host and port) to a local/test database, and leave PGHOSTADDR/PGSERVICE/\n"
        "PGSERVICEFILE/PGSYSCONFDIR unset. See tests/_db_safety.py and src/db_safety.py.\n"
        "===================================================================\n"
    )


def enforce_test_database() -> str:
    """Validate ``TEST_DATABASE_URL`` and pin the environment to it.

    MUST run before ``src.config.settings`` is imported. Returns the validated
    async test URL. Aborts the process on any missing/unsafe configuration.
    """
    test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not test_url:
        _abort("TEST_DATABASE_URL is not set.")

    ok, why = _classify(test_url)
    if not ok:
        _abort(f"TEST_DATABASE_URL is not a recognised test database: {why}.")

    # Require the sync URL explicitly — do NOT derive it. src.db.session rewrites
    # ":5432/"->":6543/" (Supabase pooler) on the sync URL, so a silently-derived
    # localhost:5432 sync URL would target a port a plain local Postgres doesn't
    # expose and break every SyncSessionLocal/sync_engine test. The dev/CI provide
    # it exactly as they provide DATABASE_URL_SYNC.
    test_sync = os.environ.get("TEST_DATABASE_URL_SYNC", "").strip()
    if not test_sync:
        _abort("TEST_DATABASE_URL_SYNC is not set.")
    ok_sync, why_sync = _classify(test_sync)
    if not ok_sync:
        _abort(f"TEST_DATABASE_URL_SYNC is not a recognised test database: {why_sync}.")

    redirects = ambient_redirects()
    if redirects:
        _abort(f"{redirects} set: libpq would route even an explicit test DSN "
               "elsewhere. Unset them for the test run.")

    # Discard whatever the shared .env held — the run uses ONLY the validated
    # test URLs from here on, so a DATABASE_URL pointing at prod is inert.
    os.environ["DATABASE_URL"] = test_url
    os.environ["DATABASE_URL_SYNC"] = test_sync
    os.environ["DATABASE_URL_MIGRATE"] = test_sync
    os.environ["ENVIRONMENT"] = "test"
    return test_url


def assert_engine_is_test(url: str) -> None:
    """Belt check for use AFTER the engine exists (e.g. in ``pytest_configure``
    and immediately before destructive teardown): confirm a live engine URL
    still resolves to a validated test database. Aborts otherwise."""
    ok, why = _classify(url)
    if not ok:
        _abort(f"Live DB engine is NOT a test database: {why}.")
    # Re-checked at the point of destruction: set mid-run, these would reroute
    # the engine's next connection whatever its URL says (Codex safety-PR review).
    redirects = ambient_redirects()
    if redirects:
        _abort(f"{redirects} set: libpq would route the test engine elsewhere.")
