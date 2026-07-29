import os
from logging.config import fileConfig

from alembic import context

# Pull DATABASE_URL_SYNC from environment (or .env)
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

load_dotenv()

config = context.config

# Override sqlalchemy.url from environment.
#
# RLS cutover (Phase 3): migrations need DDL/owner privileges, which the runtime
# roles (bridgeleads_app / bridgeleads_system) deliberately lack. Prefer
# DATABASE_URL_MIGRATE (the owner role) when set; fall back to DATABASE_URL_SYNC
# so pre-cutover environments — where workers + Alembic share one role — are
# unchanged. Do NOT point DATABASE_URL_SYNC at bridgeleads_system without also
# setting DATABASE_URL_MIGRATE, or `alembic upgrade` would run as a non-DDL role.
migrate_url = os.getenv("DATABASE_URL_MIGRATE") or os.getenv("DATABASE_URL_SYNC")
if migrate_url:
    config.set_main_option("sqlalchemy.url", migrate_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic autogenerate can detect them
from src.db.models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # scripts/migrate.py runs migrations under a PostgreSQL advisory lock and
    # passes its lock-holding connection in via config.attributes["connection"]
    # so the lock and the migration share ONE backend (see scripts/migrate.py).
    # When present, run on that connection. Otherwise (a bare `alembic` CLI
    # invocation) build an engine from sqlalchemy.url as before.
    connection = config.attributes.get("connection", None)
    if connection is not None:
        _run(connection)
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
