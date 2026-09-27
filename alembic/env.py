import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.db_safety import ambient_redirects, classify, db_identity

config = context.config

# Which database. NEVER from a .env file: this used to call load_dotenv(), which
# searches upward from alembic/ (not the cwd), so in a checkout whose .env names
# production any bare `alembic` run, or a test driving alembic.command, targeted
# PRODUCTION. Boot (scripts/migrate.py), CI and Railway all set the variables in
# the process environment; so must anyone else.
#
# scripts/migrate.py hands in its lock-holding connection (see run_migrations_online),
# so no URL is needed. Otherwise: prefer DATABASE_URL_MIGRATE (the owner role;
# the runtime roles bridgeleads_app / bridgeleads_system lack DDL) and fall back
# to DATABASE_URL_SYNC for environments where workers and Alembic share one role.
# Do NOT point DATABASE_URL_SYNC at bridgeleads_system without also setting
# DATABASE_URL_MIGRATE, or `alembic upgrade` would run as a non-DDL role.
def _refuse_unless_test_database(target: str) -> None:
    """Under ENVIRONMENT=test, migrate the validated test database or nothing: the
    target must classify as a test database AND be the database
    TEST_DATABASE_URL_SYNC names, and no libpq variable may reroute it."""
    problems = []
    redirects = ambient_redirects()
    if redirects:
        problems.append(f"{redirects} set (libpq would reroute the connection)")
    ok, why = classify(target)
    if not ok:
        problems.append(f"target is not a test database: {why}")
    test_sync = os.getenv("TEST_DATABASE_URL_SYNC", "").strip()
    if not test_sync:
        problems.append("TEST_DATABASE_URL_SYNC is not set")
    else:
        ok_test, why_test = classify(test_sync)
        if not ok_test:
            problems.append(f"TEST_DATABASE_URL_SYNC is not a test database: {why_test}")
        elif db_identity(target) != db_identity(test_sync):
            problems.append(
                f"target {db_identity(target)} is not TEST_DATABASE_URL_SYNC "
                f"{db_identity(test_sync)}"
            )
    if problems:
        raise RuntimeError(
            "alembic/env.py refuses to migrate under ENVIRONMENT=test: " + "; ".join(problems)
        )


_handed = config.attributes.get("connection", None)
if _handed is not None:
    _target = _handed.engine.url.render_as_string(hide_password=True)
else:
    _target = os.getenv("DATABASE_URL_MIGRATE") or os.getenv("DATABASE_URL_SYNC")
    if not _target:
        raise RuntimeError(
            "alembic/env.py: neither DATABASE_URL_MIGRATE nor DATABASE_URL_SYNC is set "
            "in the environment. env.py never reads a .env file: export the variable "
            "(or use `railway run`, or scripts/migrate.py)."
        )

# Before the URL reaches Alembic's config, the models are imported, or anything
# connects.
if os.getenv("ENVIRONMENT", "").strip().lower() == "test":
    _refuse_unless_test_database(_target)

if _handed is None:
    # Alembic's config is a ConfigParser: '%' starts an interpolation, so a
    # percent-encoded password (%40 ...) must be escaped (Codex safety-PR review).
    config.set_main_option("sqlalchemy.url", _target.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic autogenerate can detect them
from src.db.models import Base  # noqa: E402

target_metadata = Base.metadata


# Indexes that are built OUT OF BAND with CREATE INDEX CONCURRENTLY, because
# building them inline would hold ACCESS EXCLUSIVE on a large table for a full
# scan. They are declared on the models so create_all gives the test database
# one, which means autogenerate sees them missing on any database that has not
# had the manual script run and helpfully proposes a plain, blocking
# op.create_index. Exclude them: the scripts own these (Codex).
CONCURRENT_INDEXES = {
    "ix_results_duplicate_source",   # scripts/create_result_duplicate_source_index.sql
    "ix_pending_skip_trace_spent",   # migration 102, CONCURRENTLY
    "ix_pending_skip_trace_queued_frontier",  # migration 103, CONCURRENTLY
}


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "index" and name in CONCURRENT_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # scripts/migrate.py runs migrations under a PostgreSQL advisory lock and
    # passes its lock-holding connection in via config.attributes["connection"]
    # so the lock and the migration share ONE backend (see scripts/migrate.py).
    # When present, run on that connection. Otherwise (a bare `alembic` CLI
    # invocation) build an engine from sqlalchemy.url as before.
    if _handed is not None:
        _run(_handed)
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
