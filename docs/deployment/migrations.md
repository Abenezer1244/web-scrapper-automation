# Production database migrations

One path, one migrator. Read this before writing a migration or rolling back a deploy.

## How production migrates

| Service | At boot | On failure |
|---|---|---|
| api (2 replicas) | `scripts/migrate.py`: takes a session advisory lock, runs `alembic upgrade head` on the same connection, releases | Exits. The API never serves on a stale schema |
| worker, beat | `scripts/wait_for_schema.py`: waits until `alembic_version` shows this code's head, reading on the runtime role | Exits after 15 minutes so Railway restarts the boot. They never migrate |

- **The lock:** the two API replicas serialize on it. The loser waits, then runs a no-op upgrade. A crashed holder drops its connection, and Postgres releases the lock.
- **The migrating role:** `DATABASE_URL_MIGRATE` (the owner role), never the runtime role. `migrate.py` refuses the Supabase transaction pooler (`:6543`). The API deploy log shows the line `migrate: lock acquired as role <role> (from DATABASE_URL_MIGRATE)`.
- **Credentials:** worker and beat need only their runtime `DATABASE_URL_SYNC`. Migration 116 lets that role read `alembic_version`. `DATABASE_URL_MIGRATE` belongs on the api service only.
- **CI never migrates production.** `tests/test_ci_workflows.py` fails the build if a workflow names a production database or key secret.

## The compatibility rule (required for every migration)

Every migration must work with **the code of the previous release** as well as the new one (expand, then contract). The previous release's code can run against the new schema in three ways:

- API replicas and workers roll over independently.
- A Railway rollback redeploys old code onto the migrated database.
- `wait_for_schema.py` lets old code start on a schema that is ahead.

In practice:
- Add columns as nullable or with a default. Backfill in a later step if needed.
- Never rename or drop a column, table or enum value in the same release that stops using it. Ship the code that stops reading it first, then the drop in a later release.
- Make new constraints `NOT VALID` first and validate them separately when existing rows might violate them.
- `CREATE INDEX CONCURRENTLY` must use `IF NOT EXISTS` and handle a leftover INVALID index; `033` and `062` show the pattern.

Known limit: an `alembic_version` value this code does not recognize is treated
as "ahead" (old code cannot tell a newer release's revision from a typo), so a
hand-edited bogus revision would let worker and beat start. Never edit
`alembic_version` by hand.

## Rollback

- **Default to a forward fix.** Ship a new migration that corrects the problem.
- **Redeploying the previous Railway deployment is safe only if the compatibility rule held.** Worker and beat log `database is AHEAD of this code` and start.
- **Never run `alembic downgrade` against production.** Downgrades are untested on production data and can drop data.

## When a deploy's migration fails

1. Open the api deployment's logs on Railway and find the lines starting `migrate:` and the Alembic traceback.
2. Worker and beat log `wait_for_schema: schema behind ...`. After 15 minutes they exit and Railway restarts them, up to its retry limit. They are not running jobs on a stale schema; that is intended.
3. Fix forward: push a corrected migration. A transactional migration that failed left nothing applied, so the next deploy starts clean.
4. An interrupted `CREATE INDEX CONCURRENTLY` can leave an INVALID index. Check with
   `SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid;`
   A missing or invalid index can also just be one that is still building. Drop it only once nothing is building it, then redeploy.

## Tests

`tests/test_migrate_lock.py` runs against fresh scratch databases on the test server. It covers:
- two runners blocked on the lock both reach head;
- a rerun is a no-op;
- a failed migration releases the lock and a restart reaches head;
- a killed lock holder releases the lock;
- the lock-wait budget fails closed;
- `wait_for_schema` behind, at head, ahead and timing out;
- the runtime role seeing the revision only through 116.
