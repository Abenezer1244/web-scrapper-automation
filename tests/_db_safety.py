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
  * ``ENVIRONMENT`` is forced to ``"test"``.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlparse

# A database is accepted as a test DB only if BOTH hold:
#   1. its name ends with one of these suffixes, and
#   2. its host is localhost OR explicitly allowlisted (TEST_DB_HOST_ALLOWLIST).
TEST_DB_NAME_SUFFIXES: tuple[str, ...] = ("_test", "_testing")
# "" covers a UNIX-socket / hostless DSN, which is inherently local.
_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", ""})
# libpq / asyncpg / psycopg2 honour these DSN query params and they OVERRIDE the
# host/db parsed from the URL authority+path. A DSN like
#   postgresql+asyncpg://localhost/bridgeleads_test?host=prod&dbname=postgres
# would pass a naive authority/path check yet actually connect to prod. We refuse
# any of them outright — a test DSN never needs to redirect host/db via query.
_FORBIDDEN_QUERY_KEYS: frozenset[str] = frozenset(
    {"host", "hostaddr", "port", "dbname", "database"}
)


def _host_allowlist() -> set[str]:
    raw = os.environ.get("TEST_DB_HOST_ALLOWLIST", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _db_name(url: str) -> str:
    path = urlparse(url).path or ""
    return path.lstrip("/").split("?", 1)[0]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _forbidden_query_keys(url: str) -> set[str]:
    """Connection-overriding query keys present in the DSN (case-insensitive)."""
    keys = {k.lower() for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)}
    return keys & _FORBIDDEN_QUERY_KEYS


def _classify(url: str) -> tuple[bool, str]:
    """Return ``(is_test_db, reason_if_not)``."""
    bad_keys = _forbidden_query_keys(url)
    if bad_keys:
        return False, (
            f"DSN query overrides the connection target via {sorted(bad_keys)} — "
            "host/db redirection in a test DSN is refused"
        )
    name = _db_name(url)
    host = _host(url)
    if not any(name.endswith(s) for s in TEST_DB_NAME_SUFFIXES):
        return False, (
            f"database name {name!r} does not end with one of {TEST_DB_NAME_SUFFIXES}"
        )
    if host not in _LOCAL_HOSTS and host not in _host_allowlist():
        return False, (
            f"host {host!r} is not local and not in TEST_DB_HOST_ALLOWLIST "
            f"({sorted(_host_allowlist())})"
        )
    return True, ""


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
        "Set TEST_DATABASE_URL (and optionally TEST_DATABASE_URL_SYNC) to a\n"
        "local/test database. See tests/_db_safety.py.\n"
        "===================================================================\n"
    )


def _derive_sync(async_url: str) -> str:
    """Best-effort async->sync DSN for the optional TEST_DATABASE_URL_SYNC
    default: swap the asyncpg driver for psycopg2. The result is validated by
    the same test-DB classifier, so a bad derivation still aborts."""
    return async_url.replace("+asyncpg", "+psycopg2")


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

    test_sync = os.environ.get("TEST_DATABASE_URL_SYNC", "").strip() or _derive_sync(test_url)
    ok_sync, why_sync = _classify(test_sync)
    if not ok_sync:
        _abort(f"TEST_DATABASE_URL_SYNC is not a recognised test database: {why_sync}.")

    # Discard whatever the shared .env held — the run uses ONLY the validated
    # test URLs from here on, so a DATABASE_URL pointing at prod is inert.
    os.environ["DATABASE_URL"] = test_url
    os.environ["DATABASE_URL_SYNC"] = test_sync
    os.environ["ENVIRONMENT"] = "test"
    return test_url


def assert_engine_is_test(url: str) -> None:
    """Belt check for use AFTER the engine exists (e.g. in ``pytest_configure``
    and immediately before destructive teardown): confirm a live engine URL
    still resolves to a validated test database. Aborts otherwise."""
    ok, why = _classify(url)
    if not ok:
        _abort(f"Live DB engine is NOT a test database: {why}.")
