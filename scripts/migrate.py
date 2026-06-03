"""Run Alembic migrations under a PostgreSQL advisory lock.

Why this exists
---------------
Railway runs MULTIPLE API replicas, and rolling deploys overlap old + new
instances. ``start.sh`` runs ``alembic upgrade head`` on API boot, so two fresh
replicas can race the SAME revision: both read ``alembic_version = 032``, both
run ``upgrade 032 -> 033``, one wins, and the loser's
``UPDATE alembic_version WHERE version='032'`` matches 0 rows. Alembic then
aborts that boot with ``FAILED ... expected to match one row ... 0 found`` and
the fail-closed guard refuses to start uvicorn. It self-heals today only because
the DDL is transactional (loser rolls back, Railway retries, next boot sees head
and no-ops). A non-atomic future migration (``CREATE INDEX CONCURRENTLY``, a
multi-statement backfill) could fail far worse, and a plain ``CREATE INDEX``
already takes an ACCESS EXCLUSIVE lock that blocks writes during the overlap.

This wrapper serializes the runners: one replica holds a session-level advisory
lock and runs ``alembic upgrade head``; the others block until the lock frees,
then run a no-op upgrade and start cleanly.

Design notes (the non-obvious bits)
-----------------------------------
- **The lock and the migration run on the SAME connection.** Alembic is invoked
  in-process via ``command.upgrade`` with ``config.attributes["connection"]`` set
  to the lock-holding connection (``alembic/env.py`` honours it). If we instead
  held the lock on one connection and ran ``alembic`` as a subprocess on another,
  a dropped lock connection (pooler/NAT idle timeout) would release the lock
  while the migration was still running — letting a second replica start a
  concurrent migration. Sharing one connection means the lock cannot outlive,
  nor be outlived by, the migration.
- **No AUTOCOMMIT on our connection.** We leave transaction control to Alembic's
  normal per-migration semantics instead of forcing autocommit. Most migrations
  run inside a transaction and roll back cleanly on failure; some deliberately
  opt out via Alembic's ``autocommit_block()`` for ``CREATE INDEX CONCURRENTLY``
  (e.g. migration 033) and are intentionally non-atomic — those must be written
  idempotently/reentrantly, and the advisory lock matters MOST for them since a
  half-built CONCURRENTLY index from a racing replica is the worst case. We
  acquire the lock in its own tiny transaction and commit immediately — a
  *session-level* advisory lock persists across that commit and every commit or
  autocommit-block Alembic makes, so the connection is never left "idle in
  transaction" holding the lock.
- **Session-capable URL is required.** Supabase distinguishes session mode
  (``...pooler.supabase.com:5432`` or direct ``db.<ref>.supabase.co:5432``) from
  transaction mode (``...pooler.supabase.com:6543``). Session-level advisory
  locks are NOT safe through transaction pooling (each statement may land on a
  different backend). ``_migration_url`` validates this and fails closed rather
  than silently locking against a transaction pooler.
- **Crash safety.** If the holder process dies, Postgres drops the connection and
  releases the lock automatically — a crashed migration never wedges the next
  deploy.
- **Bounded wait.** Acquisition polls ``pg_try_advisory_lock`` with jittered
  backoff up to a budget, then fails closed so Railway restarts the boot instead
  of hanging forever. A legitimately long migration on the winner holds the lock
  while losers wait; if a migration can exceed the budget, move migrations to a
  singleton release step (see start.sh note) rather than every-replica-on-boot.

The ``(classid, objid)`` two-int lock form lives in a SEPARATE lock namespace
from the single-bigint ``pg_advisory_lock(key)`` that
``src/workers/daily_scrape.py`` uses for per-county locks, so the two can never
collide regardless of value.
"""

import os
import random
import sys
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

# Fixed advisory-lock coordinates. classid is an arbitrary project constant
# ('BL' = BridgeLeads); objid 1 = schema-migration coordination. Every replica
# must use the SAME pair to serialize against each other — treat as constant.
LOCK_CLASSID = 0x424C  # 'BL'
LOCK_OBJID = 1         # schema migrations

# Max time to wait for the lock before failing closed (seconds). Long enough for
# a slow legitimate migration on the winning replica; short enough that a stuck
# holder triggers a restart instead of an indefinite hang.
LOCK_WAIT_BUDGET_S = 900
# Jittered poll interval [base, base + spread] so losing replicas do not retry
# in lockstep and stampede the pooler.
POLL_BASE_S = 2.0
POLL_SPREAD_S = 1.5

ALEMBIC_INI = os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")


def _migration_url() -> str:
    """Resolve the Alembic DB URL and verify it is session-capable.

    Mirrors alembic/env.py: prefer DATABASE_URL_MIGRATE (owner/DDL role), else
    DATABASE_URL_SYNC. Rejects a Supabase transaction-pooler URL (:6543) because
    session-level advisory locks are unsafe through transaction pooling.
    Non-Supabase hosts (local docker-compose, self-hosted) pass through — only
    the known-unsafe Supabase transaction port is hard-rejected.
    """
    raw = os.getenv("DATABASE_URL_MIGRATE") or os.getenv("DATABASE_URL_SYNC")
    if not raw:
        sys.stderr.write(
            "migrate: neither DATABASE_URL_MIGRATE nor DATABASE_URL_SYNC is set\n"
        )
        sys.exit(1)

    host = (make_url(raw).host or "").lower()
    port = make_url(raw).port
    is_supabase_pooler = host.endswith(".pooler.supabase.com")
    is_supabase_direct = host.startswith("db.") and host.endswith(".supabase.co")

    if is_supabase_pooler and port == 6543:
        sys.stderr.write(
            "migrate: DATABASE_URL_MIGRATE/SYNC points at the Supabase TRANSACTION "
            "pooler (:6543); session advisory locks are unsafe there. Use the "
            "session pooler (:5432 on the same pooler host) or the direct host.\n"
        )
        sys.exit(1)
    if (is_supabase_pooler or is_supabase_direct) and port not in (5432, None):
        sys.stderr.write(
            f"migrate: unexpected Supabase migration port {port}; expected 5432 "
            "(session mode).\n"
        )
        sys.exit(1)
    return raw


def _acquire(conn) -> bool:
    """Poll for the advisory lock until acquired or the budget is exhausted.

    The lock is taken in its own transaction which is committed immediately; the
    session-level lock survives the commit, so the connection does not sit idle
    in a transaction while Alembic runs.
    """
    deadline = time.monotonic() + LOCK_WAIT_BUDGET_S
    while True:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:c, :o)"),
            {"c": LOCK_CLASSID, "o": LOCK_OBJID},
        ).scalar()
        conn.commit()
        if got:
            return True
        if time.monotonic() >= deadline:
            sys.stderr.write(
                "migrate: could not acquire the migration lock within "
                f"{LOCK_WAIT_BUDGET_S}s — another replica is still migrating. "
                "Failing closed so this boot restarts.\n"
            )
            return False
        print(
            "migrate: migration lock held by another replica; waiting...",
            flush=True,
        )
        time.sleep(POLL_BASE_S + random.uniform(0, POLL_SPREAD_S))  # noqa: S311 — jitter, not crypto


def main() -> int:
    engine = create_engine(
        _migration_url(),
        poolclass=NullPool,
        connect_args={"connect_timeout": 10},
    )
    # One real connection (NullPool) both HOLDS the lock and RUNS the migration.
    conn = engine.connect()
    acquired = False
    try:
        acquired = _acquire(conn)
        if not acquired:
            return 1

        print("migrate: lock acquired; running 'alembic upgrade head'", flush=True)
        cfg = Config(ALEMBIC_INI)
        cfg.attributes["connection"] = conn  # env.py runs migrations on this conn
        command.upgrade(cfg, "head")  # raises on failure -> propagates, fail-closed
        print("migrate: migrations applied", flush=True)
        return 0
    finally:
        if acquired:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:c, :o)"),
                    {"c": LOCK_CLASSID, "o": LOCK_OBJID},
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 — best-effort; close releases it anyway
                sys.stderr.write(f"migrate: advisory unlock failed (ignored): {exc}\n")
        conn.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
