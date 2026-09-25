"""Contact lookup action ledger — SCHEMA ONLY (Phase 1b-1a).

Creates the durable home for a "look up contacts" action: the action, one row per
quoted lead, and an append-only event log. Nothing writes these tables yet. The
API route lands in 1b-1c and the writers in 1b-2, which is deliberate — this
migration is the part that has to be right before anything can depend on it.

WHAT IS AND IS NOT HERE
  * `results.last_trace_outcome` is added as a COLUMN ONLY: no writer, no
    default, no NOT NULL, no trigger. It means UNKNOWN until 1b-2 integrates the
    eight existing paid-state writers (finding 16-3). **NULL must never be read
    as `provider_rejected`.** `tracerfy_ingest.py` writes
    `results.skip_trace_status='errored'` for provider-accepted-but-unmatched
    work, which is BILLABLE; retrying that as if it were a pre-submit rejection
    buys a lookup the customer already paid for. That ambiguity is the whole
    reason 15-3 asked for this column, and it is not resolved until the writers
    ship.
  * `pending_skip_trace_rows.action_id` is added nullable and indexed. 1b-0
    removed it as dead code; it is not dead (finding 16-2). A Tracerfy batch
    spans tenants and actions, and `SkipTraceQueue` stores only the FIRST row's
    tenant/job metadata, so ingest rebuilds attribution from the pending rows.
    Without this column a hit, miss, unmatched, reuse or release cannot be tied
    back to the action that bought it. The scrape path leaves it NULL, so a
    concurrent scrape is never counted as an action's work.

TENANT-CARRYING COMPOSITE FOREIGN KEYS (finding 16-1)
  Every child row carries `user_id` and references its parent by
  `(parent_id, user_id)`, so a row can never point at another account's action,
  job or result. That requires a UNIQUE `(id, user_id)` on each parent. Only
  `scraper_batches` had one (`uq_scraper_batches_id_user`, migration 050);
  15-9 named `results` alone and missed `jobs`, so the FK it specified could not
  have been created at all.

  `jobs` and `results` are the two hottest tables in the product (~172k results),
  so their indexes are built CONCURRENTLY inside an autocommit block and only
  then attached with `ADD CONSTRAINT ... USING INDEX`, which reuses the finished
  index and does NOT rescan the table. The attach is still table-level DDL taking
  a brief ACCESS EXCLUSIVE lock, hence the `lock_timeout` around it: it should
  wait rather than queue behind a long reader and stall every writer.

  This is the reverse of migration 079's choice for `jobs`, which deliberately
  used a unique INDEX rather than a constraint to avoid exactly that lock. The
  difference is that a foreign key requires a real constraint; an index alone
  cannot be an FK target. So the lock is unavoidable here and is instead made as
  short as possible.

RESTART SAFETY
  A CONCURRENTLY build that fails leaves an INVALID index behind, forever, and
  `IF NOT EXISTS` would then see the name, skip the build, and record this
  migration as applied while the constraint it exists for was absent. Each build
  therefore verifies the existing index BY IDENTITY (unique, valid, key columns,
  exact column names) and rebuilds it otherwise — the 098/100 pattern.

  Dropping an invalid index without first consulting
  `pg_stat_progress_create_index` is safe HERE and only here, because migrations
  are serialized by the advisory lock in `scripts/migrate.py`, which `start.sh`
  runs on every boot. Inside that lock an invalid index is a dead one and never a
  build still in progress. **If anything ever migrates with bare
  `alembic upgrade`, that argument collapses** and these drops must start
  checking progress first. Outside a migration the opposite rule holds:
  `indisvalid = false` means BUILDING *or* DEAD.

THE DISPOSITION TRIGGER (finding 16-6)
  Grants bound WHAT a role may touch and RLS bounds WHICH ROWS. Neither can say
  "the API may create an initial verdict but may never transition one", so
  without a trigger the API could write a terminal disposition or fabricate an
  exclusion. The trigger is the actual guarantee, because — per migration 023's
  rationale, the one trigger precedent in this repo — a trigger fires regardless
  of role privileges, including for BYPASSRLS and superuser roles.

  The discriminator is the tenant GUC. The API always sets `app.current_user_id`
  (`src/api/deps.py`, transaction-scoped, re-applied by the `after_begin`
  listener in `src/db/session.py`); the worker uses `system_sync_session()` and
  sets no GUC at all. So a non-empty GUC means "user-scoped session" = the API.

  🛑 CONSEQUENCE FOR 1b-2, do not lose: a worker that opens its session with
  `rls_sync_session(user_id)` DOES set the GUC and will therefore be refused by
  this trigger exactly as the API is. The action worker must use
  `system_sync_session()` for its transitions. It fails closed (a refusal, not a
  silent wrong write), which is the correct direction, but it will look like a
  puzzling permission error if this note is not read first.

  Plain `LANGUAGE plpgsql`, NOT `SECURITY DEFINER`, following 023. A definer
  function would also have to be owned by a BYPASSRLS role or
  `scripts/apply_rls_force.sql:56-70` hard-fails the whole convergence script.

  It is NOT mirrored in `src/db/models.py`, and the round-16 finding that asked
  for a mirror (16-12) was withdrawn after checking: NOTHING in this repository
  calls `create_all`. The local rig and CI both build the test schema with
  `alembic upgrade head`, so this trigger is present in every test database and a
  mirror would be dead code. Several comments elsewhere (models.py,
  alembic/env.py, migrations 049 and 089) still claim create_all is the test
  path; they are stale, and agreeing with each other is what made them
  persuasive.

  A SECOND trigger, `contact_lookup_action_events_guard`, bounds what a
  user-scoped session may APPEND to the event log. Append-only grants stop the
  API editing history and cannot stop it writing a fictional entry.

ALSO REQUIRED OUTSIDE THIS FILE
  `scripts/provision_rls_roles.sql` (grants + the `$verify$` allowlist),
  `scripts/apply_rls_cutover_policies.sql` (the role-targeted `_app`/`_system`
  policies) and `scripts/apply_rls_force.sql` (the `tbls[]` array, or FORCE never
  reaches these tables — finding 16-11). The inline grants below exist so a table
  created after those scripts last ran is still usable; the scripts remain
  authoritative.

Revision ID: 101
Revises: 100
Create Date: 2026-09-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision = "101"
down_revision = "100"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=False)

# Tenant predicate, byte-identical to the one migration 065 uses. `true` is
# missing_ok, so an unset GUC yields '' rather than raising.
_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)

_ACTION_TABLES = (
    "contact_lookup_actions",
    "contact_lookup_action_results",
    "contact_lookup_action_events",
)

# An action's own lifecycle. `dispatching` is written by the API at confirm time;
# everything after it belongs to the worker or the reconciler (15-2).
_ACTION_STATUSES = (
    "dispatching", "running", "claimed", "settled", "failed", "expired",
)

# Every verdict a quoted lead can hold. `quoted` is the only INITIAL state that
# is not already terminal; the exclusions are decided at quote time and never
# move again. The terminal answers (15-4) exist so the status page can say what
# happened without reading pending_skip_trace_rows, which the API cannot see.
_DISPOSITIONS = (
    "quoted",
    # decided by the worker
    "newly_queued", "reused", "already_answered", "in_progress_elsewhere",
    "ineligible", "released", "abandoned",
    # decided at quote time, by the planner
    "excluded_no_address", "excluded_placeholder_address",
    "excluded_settled_code_violation", "excluded_atip_policy",
    "excluded_not_traceable",
    # terminal outcomes (15-4)
    "answered_hit", "answered_miss", "unmatched_billable", "errored_unsubmitted",
)

# What the API is permitted to write at confirm time. Anything else — every
# worker verdict and every terminal answer — is refused by the trigger.
_API_INITIAL_DISPOSITIONS = (
    "quoted",
    "excluded_no_address", "excluded_placeholder_address",
    "excluded_settled_code_violation", "excluded_atip_policy",
    "excluded_not_traceable",
)

# What actually happened to the last lookup bought for a lead. NULL = unknown.
# The split that matters: `provider_rejected` was never charged and may be
# retried; `provider_accepted_unmatched` WAS charged and must never be.
_TRACE_OUTCOMES = (
    "provider_rejected",
    "provider_accepted_unmatched",
    "provider_answered_hit",
    "provider_answered_miss",
    "locally_cancelled",
)


def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


_TRIGGER_FN = f"""
CREATE OR REPLACE FUNCTION contact_lookup_action_results_guard()
RETURNS trigger AS $fn$
DECLARE uid TEXT;
BEGIN
    -- '' means no tenant GUC was set, i.e. a system/worker session. The API
    -- always sets it. See src/api/deps.py and src/db/session.py.
    uid := COALESCE(NULLIF(current_setting('app.current_user_id', true), ''), '');
    IF uid = '' THEN
        -- No tenant context. That is the worker's system_sync_session(), but
        -- only when it really is the worker's ROLE (Codex round 20): the API's
        -- own sync DSN logs in as bridgeleads_app too (verified in production
        -- 2026-09-25), so an API code path that ever opened a system session
        -- would otherwise pass here as the worker. The owner / migration / ops
        -- roles, which bypass RLS anyway, are the only other roles let through.
        IF current_user = 'bridgeleads_system' OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles r
             WHERE r.rolname = current_user AND (r.rolsuper OR r.rolbypassrls)
        ) THEN
            RETURN COALESCE(NEW, OLD);
        END IF;
        RAISE EXCEPTION
            '%: no tenant context (app.current_user_id is empty) and role % is '
            'not the worker role. Worker hops go through system_sync_session() '
            'as bridgeleads_system; request paths set the tenant.',
            TG_TABLE_NAME, current_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.disposition NOT IN ({_sql_list(_API_INITIAL_DISPOSITIONS)}) THEN
            RAISE EXCEPTION
                'contact_lookup_action_results: a user-scoped session may only '
                'create an initial disposition, not %. Worker verdicts and '
                'terminal answers are written by the worker through '
                'system_sync_session().', NEW.disposition
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        -- Server-owned, never taken from the request: a server_default is
        -- skipped whenever the caller supplies a value (Codex round 19).
        NEW.decided_at := now();
        RETURN NEW;
    END IF;
    -- UPDATE or DELETE from a user-scoped session: never.
    RAISE EXCEPTION
        'contact_lookup_action_results: a user-scoped session may not transition '
        'a disposition (% -> %). Only the worker may, and only through '
        'system_sync_session().', OLD.disposition, COALESCE(NEW.disposition, '<deleted>')
        USING ERRCODE = 'insufficient_privilege';
END;
$fn$ LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp;
"""

# The event log is the record a disputed charge is argued from, so "append-only"
# has to mean the API cannot APPEND A LIE either. A grant can stop it editing
# history and cannot stop it writing a fictional entry, so the same GUC test
# bounds what a user-scoped session may append: the API's own
# created -> dispatching hop and nothing else. Every later hop belongs to the
# worker or the reconciler, and a per-lead hop (a release, an abandonment)
# carries a result_id the API has no business asserting (Codex review).
_EVENT_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION contact_lookup_action_events_guard()
RETURNS trigger AS $fn$
DECLARE uid TEXT;
BEGIN
    uid := COALESCE(NULLIF(current_setting('app.current_user_id', true), ''), '');
    IF uid = '' THEN
        -- No tenant context. That is the worker's system_sync_session(), but
        -- only when it really is the worker's ROLE (Codex round 20): the API's
        -- own sync DSN logs in as bridgeleads_app too (verified in production
        -- 2026-09-25), so an API code path that ever opened a system session
        -- would otherwise pass here as the worker. The owner / migration / ops
        -- roles, which bypass RLS anyway, are the only other roles let through.
        IF current_user = 'bridgeleads_system' OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles r
             WHERE r.rolname = current_user AND (r.rolsuper OR r.rolbypassrls)
        ) THEN
            RETURN COALESCE(NEW, OLD);
        END IF;
        RAISE EXCEPTION
            '%: no tenant context (app.current_user_id is empty) and role % is '
            'not the worker role. Worker hops go through system_sync_session() '
            'as bridgeleads_system; request paths set the tenant.',
            TG_TABLE_NAME, current_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION
            'contact_lookup_action_events is append-only: a user-scoped session '
            'may not % an event.', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.to_status <> 'dispatching' OR NEW.from_status IS NOT NULL
       OR NEW.result_id IS NOT NULL OR NEW.lease_token IS NOT NULL
       OR NEW.reason IS NOT NULL THEN
        RAISE EXCEPTION
            'contact_lookup_action_events: a user-scoped session may only append '
            'the initial dispatching event (got from=% to=% result_id=% '
            'lease_token=% reason=%). Every later hop is written by the worker '
            'through system_sync_session(), the fencing lease is the worker''s '
            'alone, and the API''s own hop carries no free-text reason.',
            NEW.from_status, NEW.to_status, NEW.result_id, NEW.lease_token,
            NEW.reason
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- The shape being right is not enough: the ACTION must still be at the hop
    -- this event claims to record. Without this an API session can append a
    -- fabricated "initial" event to an action that is already running, settled
    -- or failed, which is history a billing dispute would be argued from
    -- (Codex). Repeated appends while the action is genuinely still
    -- `dispatching` stay legal, because a dispatch retry is a real path.
    --
    -- FOR SHARE, not a plain read (Codex round 19): at READ COMMITTED an
    -- unlocked read could see `dispatching`, the worker could then commit
    -- `running`, and this event would land in history AFTER it. FOR SHARE
    -- conflicts with the worker's status UPDATE, so one waits for the other and
    -- the loser re-reads the committed row. FOR KEY SHARE would not do: a status
    -- change is a non-key update and does not conflict with it. The API can
    -- take this lock because it holds UPDATE on a column of the table
    -- (dispatched_at) and a tenant UPDATE policy.
    --
    -- Schema-qualified (Codex round 20): unqualified, a session could create
    -- pg_temp.contact_lookup_actions reporting `dispatching` and append an
    -- initial event for a real action that is already running or settled.
    PERFORM 1 FROM public.contact_lookup_actions a
     WHERE a.id = NEW.action_id
       AND a.user_id = NEW.user_id
       AND a.status = 'dispatching'
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'contact_lookup_action_events: the initial event may only be appended '
            'while its action is still dispatching. Action % is not.', NEW.action_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- Server-owned, never taken from the request (see decided_at above).
    NEW.at := now();
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp;
"""

# The action row itself (Codex round 19). The API owns exactly one hop (15-2):
# it CREATES the action in `dispatching`, and once a publish succeeds it stamps
# `dispatched_at` a single time. It never changes `status`: a publish failure
# leaves the action `dispatching` for the reconciler, and every later hop is
# the worker's. Without this guard the API's table-wide INSERT could create an
# action already `claimed`, holding a lease and invented counts, and its UPDATE
# could resurrect a `failed` action or settle a live one.
#
# Timestamps are OVERWRITTEN with now(), not validated: a server_default is
# skipped whenever the caller supplies a value, and an API-written future
# `created_at` would keep a `dispatching` action from ever expiring, stranding
# its quoted rows. `dispatched_at` is set once and never restamped, so a retry
# cannot push back the apparent dispatch clock.
_ACTION_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION contact_lookup_actions_guard()
RETURNS trigger AS $fn$
DECLARE uid TEXT;
BEGIN
    uid := COALESCE(NULLIF(current_setting('app.current_user_id', true), ''), '');
    IF uid = '' THEN
        -- No tenant context. That is the worker's system_sync_session(), but
        -- only when it really is the worker's ROLE (Codex round 20): the API's
        -- own sync DSN logs in as bridgeleads_app too (verified in production
        -- 2026-09-25), so an API code path that ever opened a system session
        -- would otherwise pass here as the worker. The owner / migration / ops
        -- roles, which bypass RLS anyway, are the only other roles let through.
        IF current_user = 'bridgeleads_system' OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles r
             WHERE r.rolname = current_user AND (r.rolsuper OR r.rolbypassrls)
        ) THEN
            RETURN COALESCE(NEW, OLD);
        END IF;
        RAISE EXCEPTION
            '%: no tenant context (app.current_user_id is empty) and role % is '
            'not the worker role. Worker hops go through system_sync_session() '
            'as bridgeleads_system; request paths set the tenant.',
            TG_TABLE_NAME, current_user
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'dispatching' OR NEW.status_reason IS NOT NULL
           OR NEW.dispatched_at IS NOT NULL OR NEW.started_at IS NOT NULL
           OR NEW.claimed_at IS NOT NULL OR NEW.settled_at IS NOT NULL
           OR NEW.lease_token IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR NEW.claimed_count <> 0 OR NEW.reused_count <> 0
           OR NEW.newly_queued_count <> 0 OR NEW.billable_rows <> 0
           OR NEW.tracerfy_credits <> 0 THEN
            RAISE EXCEPTION
                'contact_lookup_actions: a user-scoped session may only create an '
                'action in its initial dispatching state (got status=%), with no '
                'reason, no lease, no worker timestamps and no worker counts.',
                NEW.status
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        NEW.created_at := now();
        NEW.status_changed_at := now();
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- Compared as whole rows minus the one column the API may set, so a
        -- column added later is covered without anyone remembering this list.
        IF OLD.status <> 'dispatching' OR OLD.dispatched_at IS NOT NULL
           OR NEW.dispatched_at IS NULL
           OR (to_jsonb(NEW) - 'dispatched_at')
              IS DISTINCT FROM (to_jsonb(OLD) - 'dispatched_at') THEN
            RAISE EXCEPTION
                'contact_lookup_actions: a user-scoped session may only stamp '
                'dispatched_at, once, while the action is still dispatching '
                '(action % is %, dispatched_at %).', OLD.id, OLD.status,
                OLD.dispatched_at
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        NEW.dispatched_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        'contact_lookup_actions: a user-scoped session may not delete an action.'
        USING ERRCODE = 'insufficient_privilege';
END;
$fn$ LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp;
"""


def _build_parent_unique(conn, table: str, index: str) -> None:
    """CREATE UNIQUE INDEX CONCURRENTLY (id, user_id), restart-safe.

    Verifies an existing index BY IDENTITY rather than by name: `CREATE ... IF
    NOT EXISTS` treats any same-named index as success, so a non-unique one, one
    on the wrong columns, or an INVALID corpse left by a failed build would leave
    this migration recorded as applied while the FK target it exists for is
    absent.
    """
    existing = conn.execute(text(
        "SELECT i.indisvalid, i.indisunique, i.indnkeyatts, "
        "       i.indexprs IS NULL AS plain_columns, "
        "       (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a "
        "            ON a.attrelid = i.indrelid AND a.attnum = k.attnum) AS cols "
        "FROM pg_class c "
        "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
        "JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
        "WHERE c.relname = :n AND cn.nspname = 'public' "
        "  AND t.relname = :t AND tn.nspname = 'public'"
    ), {"n": index, "t": table}).first()

    wrong_shape = existing is not None and not (
        existing.indisvalid
        and existing.indisunique
        and existing.indnkeyatts == 2
        and existing.plain_columns
        and list(existing.cols or []) == ["id", "user_id"]
    )
    if wrong_shape:
        conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{index}"))
        existing = None
    if existing is None:
        conn.execute(text(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {index} "
            f"ON public.{table} (id, user_id)"
        ))


def _attach_parent_unique(conn, table: str, index: str) -> None:
    """Promote the finished index to a UNIQUE constraint so an FK can target it.

    A foreign key cannot reference a bare unique index, only a constraint. USING
    INDEX adopts the index that was just built rather than rescanning the table,
    so this is metadata-only — but it is still table-level DDL holding ACCESS
    EXCLUSIVE, which on `results` blocks every reader for its duration. The
    lock_timeout makes it give up rather than queue ahead of a long reader and
    stall the whole table.

    The constraint takes the SAME name as the index deliberately: Postgres
    renames the index to the constraint name when they differ, so reusing it
    keeps one name in the catalog, in the ORM and in any diagnostic query.
    """
    already = conn.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = :c AND contype = 'u'"
    ), {"c": index}).scalar()
    if already:
        return
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text(
        f"ALTER TABLE public.{table} "
        f"ADD CONSTRAINT {index} UNIQUE USING INDEX {index}"
    ))


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))

    # ── 1. Additive columns on live tables ──────────────────────────────────
    # Both nullable with no default, so neither rewrites its table and neither
    # can fail against existing rows.
    #
    # IDEMPOTENT ON PURPOSE, and this is the subtle part (Codex review of this
    # migration). `autocommit_block()` below COMMITS the transaction it is
    # entered from, so everything in this section is durable BEFORE the index
    # build runs — while `alembic_version` is not written until the very end. If
    # the attach later times out, the next boot replays this migration from the
    # top and a plain `ADD COLUMN` would abort on "column already exists",
    # leaving the migration permanently stuck and needing a human.
    #
    # `op.add_column` has no IF NOT EXISTS, hence raw SQL. The CHECK gets the
    # catalog guard rather than a bare ADD CONSTRAINT for the same reason.
    op.execute(
        "ALTER TABLE public.results "
        "ADD COLUMN IF NOT EXISTS last_trace_outcome VARCHAR(32)"
    )
    op.execute(
        f"""
        DO $ck_lto$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_results_last_trace_outcome'
                  AND conrelid = 'public.results'::regclass
            ) THEN
                ALTER TABLE public.results
                    ADD CONSTRAINT ck_results_last_trace_outcome
                    CHECK (last_trace_outcome IS NULL
                           OR last_trace_outcome IN ({_sql_list(_TRACE_OUTCOMES)}));
            END IF;
        END
        $ck_lto$;
        """
    )
    op.execute(
        "ALTER TABLE public.pending_skip_trace_rows "
        "ADD COLUMN IF NOT EXISTS action_id UUID"
    )

    # ── 2. Parent composite uniqueness, CONCURRENTLY (16-1) ─────────────────
    # Outside the migration's transaction. If this block times out the revision
    # is left unrecorded and the whole migration is replayed, which is why every
    # step above and below it is idempotent.
    with op.get_context().autocommit_block():
        ac = op.get_bind()
        _build_parent_unique(ac, "jobs", "uq_jobs_id_user")
        _build_parent_unique(ac, "results", "uq_results_id_user")

    conn = op.get_bind()
    _attach_parent_unique(conn, "jobs", "uq_jobs_id_user")
    _attach_parent_unique(conn, "results", "uq_results_id_user")

    # ── 3. The three tables ─────────────────────────────────────────────────
    op.create_table(
        "contact_lookup_actions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        # Unique so a retried confirm resolves to the SAME action from the
        # database rather than creating a second one (15-13).
        sa.Column("quote_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(255), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        # What the customer was SHOWN. Billing never derives from this; storing
        # both is what makes quote-vs-invoice drift detectable (15-17).
        sa.Column("unit_price_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("pricing_version", sa.String(32), nullable=False),
        # A CACHE, aggregated from contact_lookup_action_results, which stays the
        # source of truth and is fully recomputable.
        sa.Column("quoted_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("claimed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("reused_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("newly_queued_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("billable_rows", sa.Integer, nullable=False, server_default="0"),
        # Advanced traces cost 2 credits but bill 1 row; the gap is recorded per
        # action so it can be priced later (decision D2).
        sa.Column("tracerfy_credits", sa.Integer, nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("quote_id", name="uq_contact_lookup_actions_quote"),
        # This table is itself a composite-FK parent for the two children below.
        sa.UniqueConstraint("id", "user_id", name="uq_contact_lookup_actions_id_user"),
        sa.ForeignKeyConstraint(
            ["job_id", "user_id"], ["jobs.id", "jobs.user_id"],
            ondelete="CASCADE", name="fk_contact_lookup_actions_job_tenant",
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_list(_ACTION_STATUSES)})",
            name="ck_contact_lookup_actions_status",
        ),
        # The one count the API writes (at confirm, the quoted set's size).
        sa.CheckConstraint(
            "quoted_count >= 0", name="ck_contact_lookup_actions_quoted_count",
        ),
    )

    op.create_table(
        "contact_lookup_action_results",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("action_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("result_id", _UUID, nullable=False),
        sa.Column("disposition", sa.String(40), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        # One verdict per lead per action. This is what makes the counts
        # derivable rather than guessed, and what stops a redelivered task
        # double-counting.
        sa.UniqueConstraint("action_id", "result_id",
                            name="uq_contact_lookup_action_results_pair"),
        sa.ForeignKeyConstraint(
            ["action_id", "user_id"],
            ["contact_lookup_actions.id", "contact_lookup_actions.user_id"],
            ondelete="CASCADE", name="fk_contact_lookup_action_results_action_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["result_id", "user_id"], ["results.id", "results.user_id"],
            ondelete="CASCADE", name="fk_contact_lookup_action_results_result_tenant",
        ),
        sa.CheckConstraint(
            f"disposition IN ({_sql_list(_DISPOSITIONS)})",
            name="ck_contact_lookup_action_results_disposition",
        ),
    )

    op.create_table(
        "contact_lookup_action_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("action_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        # Set when the hop belongs to ONE lead (a release, an abandonment), so a
        # partially claimed action can be reconstructed lead by lead and not only
        # in aggregate.
        sa.Column("result_id", _UUID, nullable=True),
        sa.Column("from_status", sa.String(40), nullable=True),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["action_id", "user_id"],
            ["contact_lookup_actions.id", "contact_lookup_actions.user_id"],
            ondelete="CASCADE", name="fk_contact_lookup_action_events_action_tenant",
        ),
    )

    # ── 4. Child-side FK indexes (16-9) ─────────────────────────────────────
    # Without these, RLS reads, cascades and reconciliation degrade as actions
    # accumulate, and a user delete has to seq-scan every child.
    op.create_index("ix_contact_lookup_actions_user", "contact_lookup_actions",
                    ["user_id"])
    op.create_index("ix_contact_lookup_actions_job_tenant", "contact_lookup_actions",
                    ["job_id", "user_id"])
    op.create_index("ix_contact_lookup_action_results_action_tenant",
                    "contact_lookup_action_results", ["action_id", "user_id"])
    op.create_index("ix_contact_lookup_action_results_result_tenant",
                    "contact_lookup_action_results", ["result_id", "user_id"])
    op.create_index("ix_contact_lookup_action_events_action_tenant",
                    "contact_lookup_action_events", ["action_id", "user_id"])
    op.create_index("ix_pending_skip_trace_action", "pending_skip_trace_rows",
                    ["action_id", "user_id"])

    # ── 5. RLS: enable + the untargeted GUC policy (the 065 pattern) ────────
    # Role-INDEPENDENT and inline, so the table is isolated from creation. The
    # role-targeted _app/_system policies live in apply_rls_cutover_policies.sql.
    for tbl in _ACTION_TABLES:
        op.execute(f"ALTER TABLE public.{tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {tbl}_user_isolation ON public.{tbl} "
            f"USING ({_GUC_PREDICATE})"
        )

    # ── 6. Grants, role-guarded so CI (no provisioned roles) is a clean no-op ─
    # GRANT ... ON ALL TABLES in provision_rls_roles.sql does not retroactively
    # cover a table created later, which is why these are inline (the 065
    # lesson). The scripts stay authoritative; these keep the gap closed.
    #
    # The API may SELECT and INSERT, never UPDATE or DELETE: it creates the
    # action and its quoted set at confirm time and never transitions a verdict.
    # Events are append-only for BOTH roles (15-8): no UPDATE, no DELETE.
    op.execute(
        """
        DO $cla_grants$
        BEGIN
            REVOKE ALL ON public.contact_lookup_actions,
                          public.contact_lookup_action_results,
                          public.contact_lookup_action_events FROM PUBLIC;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON public.contact_lookup_actions,
                              public.contact_lookup_action_results,
                              public.contact_lookup_action_events FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON public.contact_lookup_actions,
                              public.contact_lookup_action_results,
                              public.contact_lookup_action_events FROM authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                -- UPDATE on the action only, and only on dispatched_at: the one
                -- column the API writes after creating the action (15-2). A
                -- table-wide UPDATE would let the API rewrite unit_price_cents,
                -- the aggregated counts, billable_rows, the fencing lease or
                -- any timestamp, and an RLS policy could not stop it: a policy
                -- constrains WHICH ROWS, never WHICH COLUMNS (Codex review of
                -- this migration). The API never changes `status`, so it gets
                -- no grant on it (Codex round 19); contact_lookup_actions_guard
                -- enforces the same rule for every session, owner included.
                GRANT SELECT, INSERT ON public.contact_lookup_actions
                    TO bridgeleads_app;
                GRANT UPDATE (dispatched_at)
                    ON public.contact_lookup_actions TO bridgeleads_app;
                GRANT SELECT, INSERT ON public.contact_lookup_action_results
                    TO bridgeleads_app;
                GRANT SELECT, INSERT ON public.contact_lookup_action_events
                    TO bridgeleads_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE ON public.contact_lookup_actions
                    TO bridgeleads_system;
                GRANT SELECT, INSERT, UPDATE ON public.contact_lookup_action_results
                    TO bridgeleads_system;
                -- Append-only for the worker too: history is evidence in a
                -- billing dispute and must not be editable by the thing that
                -- wrote it.
                GRANT SELECT, INSERT ON public.contact_lookup_action_events
                    TO bridgeleads_system;
            END IF;
        END
        $cla_grants$;
        """
    )

    # ── 7. The disposition guard (16-6) ─────────────────────────────────────
    op.execute(_TRIGGER_FN)
    op.execute(
        "DROP TRIGGER IF EXISTS contact_lookup_action_results_guard_trg "
        "ON contact_lookup_action_results"
    )
    op.execute(
        "CREATE TRIGGER contact_lookup_action_results_guard_trg "
        "BEFORE INSERT OR UPDATE OR DELETE ON contact_lookup_action_results "
        "FOR EACH ROW EXECUTE FUNCTION contact_lookup_action_results_guard()"
    )
    op.execute(_EVENT_TRIGGER_FN)
    op.execute(
        "DROP TRIGGER IF EXISTS contact_lookup_action_events_guard_trg "
        "ON contact_lookup_action_events"
    )
    op.execute(
        "CREATE TRIGGER contact_lookup_action_events_guard_trg "
        "BEFORE INSERT OR UPDATE OR DELETE ON contact_lookup_action_events "
        "FOR EACH ROW EXECUTE FUNCTION contact_lookup_action_events_guard()"
    )
    op.execute(_ACTION_TRIGGER_FN)
    op.execute(
        "DROP TRIGGER IF EXISTS contact_lookup_actions_guard_trg "
        "ON contact_lookup_actions"
    )
    op.execute(
        "CREATE TRIGGER contact_lookup_actions_guard_trg "
        "BEFORE INSERT OR UPDATE OR DELETE ON contact_lookup_actions "
        "FOR EACH ROW EXECUTE FUNCTION contact_lookup_actions_guard()"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))

    op.execute(
        "DROP TRIGGER IF EXISTS contact_lookup_actions_guard_trg "
        "ON contact_lookup_actions"
    )
    op.execute("DROP FUNCTION IF EXISTS contact_lookup_actions_guard()")

    op.execute(
        "DROP TRIGGER IF EXISTS contact_lookup_action_events_guard_trg "
        "ON contact_lookup_action_events"
    )
    op.execute("DROP FUNCTION IF EXISTS contact_lookup_action_events_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS contact_lookup_action_results_guard_trg "
        "ON contact_lookup_action_results"
    )
    op.execute("DROP FUNCTION IF EXISTS contact_lookup_action_results_guard()")

    for tbl in _ACTION_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {tbl}_user_isolation ON public.{tbl}")

    op.drop_index("ix_pending_skip_trace_action", table_name="pending_skip_trace_rows")
    # Children first: each holds a composite FK onto contact_lookup_actions.
    op.drop_table("contact_lookup_action_events")
    op.drop_table("contact_lookup_action_results")
    op.drop_table("contact_lookup_actions")

    op.drop_column("pending_skip_trace_rows", "action_id")
    op.drop_constraint("ck_results_last_trace_outcome", "results", type_="check")
    op.drop_column("results", "last_trace_outcome")

    # Dropping the constraint drops the index it adopted. Only now, once no FK
    # references them: a live FK would refuse the drop anyway.
    op.drop_constraint("uq_results_id_user", "results", type_="unique")
    op.drop_constraint("uq_jobs_id_user", "jobs", type_="unique")
