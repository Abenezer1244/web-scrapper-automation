"""Block a worker or beat boot until the database schema matches this code.

Production has ONE migrator: the API service runs scripts/migrate.py at boot
(advisory locked, fail closed). Worker and beat never migrate. They hold no DDL
credential; they wait here, on the runtime role, until the schema this code was
built against is in place, then start.

Why wait instead of start: Railway starts api, worker and beat independently, so
"the API will have migrated by then" is a race. A worker on new code against the
old schema fails ordinary queries (a new column on `users` is named by every
`select(User)`). Serving work against a stale schema is worse than not serving.

States, read from alembic_version on the runtime role (migration 116 lets that
role see the row; before 116 is applied it sees none, which reads as "behind"):

  * ready   - every head of this code is applied. Start.
  * ahead   - the database is at a revision this code does not know: a rollback
              to an older release. Every migration is required to be backward
              compatible with the previous release (docs/deployment/migrations.md),
              so start, loudly.
  * behind  - wait and re-check, up to the budget, then exit 1 so Railway
              restarts the boot. Exiting is deliberate: a worker that cannot see
              its schema must not run jobs.
"""

import os
import random
import sys
import time

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")

# Long enough to outlast the API's own lock wait plus a slow migration; short
# enough that a deploy whose migration failed restarts instead of hanging.
WAIT_BUDGET_S = 900
POLL_BASE_S = 5.0
POLL_SPREAD_S = 2.0


def schema_state(versions: set[str], script: ScriptDirectory) -> str:
    """Classify the database's applied revisions against this code's script dir."""
    heads = set(script.get_heads())
    if heads <= versions:
        return "ready"
    for v in versions:
        try:
            script.get_revision(v)
        except Exception:  # noqa: BLE001 - alembic raises several types for an unknown id
            return "ahead"
    return "behind"


def _read_versions(engine) -> set[str]:
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))}


def main() -> int:
    # The runtime DSN only. No fallback to DATABASE_URL_MIGRATE: a worker that
    # silently reached for the owner credential would undo the point of not
    # giving it one.
    url = os.getenv("DATABASE_URL_SYNC")
    if not url:
        sys.stderr.write("wait_for_schema: DATABASE_URL_SYNC is not set\n")
        return 1
    script = ScriptDirectory.from_config(Config(ALEMBIC_INI))
    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 10})
    deadline = time.monotonic() + WAIT_BUDGET_S
    try:
        while True:
            try:
                versions = _read_versions(engine)
                state = schema_state(versions, script)
            except Exception as exc:  # noqa: BLE001 - a DB blip is "not ready yet", not a crash
                versions, state = set(), f"unreadable ({type(exc).__name__})"
            if state == "ready":
                print(f"wait_for_schema: schema at {sorted(versions)}; starting", flush=True)
                return 0
            if state == "ahead":
                print(f"wait_for_schema: WARNING database is AHEAD of this code ({sorted(versions)} "
                      f"vs heads {sorted(script.get_heads())}); starting on the rollback "
                      "compatibility rule", flush=True)
                return 0
            if time.monotonic() >= deadline:
                sys.stderr.write(
                    f"wait_for_schema: schema still {state} ({sorted(versions)}, want "
                    f"{sorted(script.get_heads())}) after {WAIT_BUDGET_S}s. The API's migration "
                    "has not landed; refusing to start so this boot restarts.\n")
                return 1
            print(f"wait_for_schema: schema {state} ({sorted(versions)}, want "
                  f"{sorted(script.get_heads())}); waiting for the API to migrate", flush=True)
            time.sleep(POLL_BASE_S + random.uniform(0, POLL_SPREAD_S))  # noqa: S311 - jitter, not crypto
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
