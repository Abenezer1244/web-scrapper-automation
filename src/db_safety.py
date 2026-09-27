"""Is this database a TEST database? One answer for the pytest guard and for Alembic.

Shared by ``tests/_db_safety.py`` (which pins the suite to a test database before
anything connects) and ``alembic/env.py`` (which refuses to migrate anything but
that database when ``ENVIRONMENT=test``). A pytest teardown has wiped PRODUCTION
twice; both callers exist so a test run can never reach it again.

Dependency-free on purpose: the pytest guard imports this BEFORE
``src.config.settings``, so it must not import settings, the engine, or anything
that reads the environment at import.

A URL is a test database only if ALL hold:
  * no query parameter that redirects the connection (libpq, psycopg2 and asyncpg
    let ``host``/``dbname``/``service``... in the query override the URL);
  * the database name ends with one of ``TEST_DB_NAME_SUFFIXES``;
  * the host is explicit AND local or in ``TEST_DB_HOST_ALLOWLIST`` (a missing host
    is filled from ``PGHOST``, which could name anything);
  * the port is explicit (a missing one is filled from ``PGPORT``).
Separately, ``ambient_redirects()`` names the libpq environment variables that
route a connection elsewhere even when host, port and database are all explicit.
"""
from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlparse

TEST_DB_NAME_SUFFIXES: tuple[str, ...] = ("_test", "_testing")
_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
# Query keys that override the target parsed from the URL. `service` pulls
# connection settings from pg_service.conf (Codex safety-PR consult, round 3).
_FORBIDDEN_QUERY_KEYS: frozenset[str] = frozenset(
    {"host", "hostaddr", "port", "dbname", "database", "service"}
)
# libpq reads these from the environment and they win over an explicit host:
# PGHOSTADDR is the address actually dialled, and a service file (named by
# PGSERVICE, found via PGSERVICEFILE / PGSYSCONFDIR) can supply one (round 4).
AMBIENT_REDIRECT_VARS: tuple[str, ...] = (
    "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR",
)


def _host_allowlist() -> set[str]:
    raw = os.environ.get("TEST_DB_HOST_ALLOWLIST", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _db_name(url: str) -> str:
    path = urlparse(url).path or ""
    return path.lstrip("/").split("?", 1)[0]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _port(url: str) -> int | None:
    try:
        return urlparse(url).port
    except ValueError:
        return None


def _forbidden_query_keys(url: str) -> set[str]:
    """Connection-overriding query keys present in the DSN (case-insensitive)."""
    keys = {k.lower() for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)}
    return keys & _FORBIDDEN_QUERY_KEYS


def classify(url: str) -> tuple[bool, str]:
    """``(is_test_db, reason_if_not)``. The reason never contains credentials."""
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
    if not host:
        return False, (
            "DSN has no explicit host; a hostless DSN can resolve to a remote DB "
            "via PGHOST — set an explicit local host (localhost/127.0.0.1)"
        )
    if host not in _LOCAL_HOSTS and host not in _host_allowlist():
        return False, (
            f"host {host!r} is not local and not in TEST_DB_HOST_ALLOWLIST "
            f"({sorted(_host_allowlist())})"
        )
    if _port(url) is None:
        return False, (
            "DSN has no explicit port; a missing port is filled from PGPORT — "
            "set it explicitly (e.g. :5432)"
        )
    return True, ""


def db_identity(url: str) -> tuple[str, int | None, str]:
    """(host, port, database): which database a URL reaches, ignoring the driver,
    the credentials and non-routing query parameters. Works on a URL rendered with
    its password masked."""
    return _host(url), _port(url), _db_name(url)


def ambient_redirects() -> list[str]:
    """The libpq routing variables set (non-empty) in this process's environment."""
    return [v for v in AMBIENT_REDIRECT_VARS if os.environ.get(v, "").strip()]


__all__ = [
    "AMBIENT_REDIRECT_VARS", "TEST_DB_NAME_SUFFIXES", "ambient_redirects", "classify",
    "db_identity",
]
