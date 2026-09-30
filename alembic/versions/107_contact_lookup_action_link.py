"""Contact-lookup action: pending rows point at their action; unbilled unmatched; the quote snapshot (107).

Phase 1b-2, step 2a-i. Schema only: no writer uses any of it until 2a-ii (the claim
writes `action_id`) and 2b/2c (the worker and the reconciler).

1. `fk_pending_skip_trace_action_tenant`: `pending_skip_trace_rows (action_id, user_id)`
   -> `contact_lookup_actions (id, user_id)` (audit2 T-5). A pending row names the
   action that bought it, and the composite key makes a row that names ANOTHER
   tenant's action impossible at the database. MATCH SIMPLE: a NULL `action_id` (the
   scrape path, and every existing row) is not checked.
   ON DELETE NO ACTION, never CASCADE: a pending row is billing evidence, so deleting
   an action must never delete it. NO ACTION is checked at the END of the statement,
   so a job or user delete, which cascades to BOTH the action and its pending rows,
   succeeds (consult r2 confirmed; tested). Added NOT VALID, then VALIDATEd in the
   same migration: every existing row is NULL, so validation is one scan with nothing
   to find.

2. `unmatched_unbilled` joins the disposition vocabulary (consult r1 V2). Billing
   bills an `unmatched` row only when Tracerfy accepted every row of its queue
   (`skip_trace_usage.py`, `accepted_all`); otherwise it is NOT billed. The action's
   verdict must say which, or the status page would claim a charge Stripe never saw.
   The CHECK is dropped and re-added NOT VALID, then validated.

3. `contact_lookup_actions.quote_snapshot` JSONB NOT NULL DEFAULT '{}' (consult r1
   V6): the quote as the customer saw it (counts, exclusions, the pinned planner
   policy of 15-14, access, trial allowance, window). The API writes it once, at
   INSERT; migration 101's actions guard already freezes every column but
   `dispatched_at` on a user-scoped UPDATE (its `to_jsonb` diff covers a new column),
   so no guard change is needed. A constant default is catalog-only (no rewrite).

Locks (all taken in the migration's ONE transaction and held until it commits):
- `contact_lookup_actions`: ACCESS EXCLUSIVE (ADD COLUMN), and SHARE ROW EXCLUSIVE as
  the FK's referenced table.
- `contact_lookup_action_results`: ACCESS EXCLUSIVE (DROP/ADD CHECK).
- `pending_skip_trace_rows`: SHARE ROW EXCLUSIVE (ADD FK), which blocks the queue's
  writers (the dispatcher, ingest, the claim) until commit. VALIDATE itself needs only
  SHARE UPDATE EXCLUSIVE, but the stronger lock is already held.
The ledger tables are empty in production and have no writer yet. The pending table
is small (~1k rows), so the whole migration is a brief block. `lock_timeout = 5s`
bounds only the wait to ACQUIRE each lock (a boot migration behind a long reader fails
fast instead of queueing every writer behind it), not how long it is held. No current
writer takes these tables in the opposite order (the dispatcher commits its claim
before the provider call), so no deadlock was found (Codex review of 2a-i).

Replay-safe and object-verified: an existing FK or CHECK of the right name is checked
BY ITS DEFINITION on the right table (an impostor is rebuilt), and an existing
`quote_snapshot` column must already be jsonb NOT NULL DEFAULT '{}' or the migration
aborts.

Forward-only in production. downgrade() exists for test databases: it refuses while
ANY action row exists (dropping `quote_snapshot` would destroy quote evidence) or any
verdict is `unmatched_unbilled` (the older CHECK would reject it).

Revision ID: 107
Revises: 106
Create Date: 2026-09-30
"""
from alembic import op
from sqlalchemy import text

revision = "107"
down_revision = "106"
branch_labels = None
depends_on = None

# This migration's OWN copy of the vocabulary (the 100/101 convention: a migration
# keeps working when application code moves on). 101's tuple plus one value.
# tests/test_contact_lookup_schema.py pins it to src.db.models.
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
    # 107: provider-accepted but unmatched in a queue that billing did NOT bill
    "unmatched_unbilled",
)
_PREVIOUS_DISPOSITIONS = tuple(d for d in _DISPOSITIONS if d != "unmatched_unbilled")

_FK = "fk_pending_skip_trace_action_tenant"
_CHECK = "ck_contact_lookup_action_results_disposition"


def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


# What pg_get_constraintdef prints for the FK we want (NO ACTION is the default and
# is not printed; " NOT VALID" is stripped before comparing).
_FK_DEF = ("FOREIGN KEY (action_id, user_id) "
           "REFERENCES contact_lookup_actions(id, user_id)")


def _fk_definition(conn) -> str | None:
    """The definition of the constraint named _FK ON pending_skip_trace_rows, or None.
    Scoped by table: a same-named constraint elsewhere is not ours."""
    return conn.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = :n AND conrelid = 'public.pending_skip_trace_rows'::regclass"
    ), {"n": _FK}).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))

    conn.execute(text(
        "ALTER TABLE public.contact_lookup_actions "
        "ADD COLUMN IF NOT EXISTS quote_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb"
    ))
    # IF NOT EXISTS would silently keep a WRONG pre-existing column: verify it.
    col = conn.execute(text(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'contact_lookup_actions' "
        "AND column_name = 'quote_snapshot'"
    )).one()
    if (col.data_type, col.is_nullable, col.column_default) != ("jsonb", "NO", "'{}'::jsonb"):
        raise RuntimeError(f"107: contact_lookup_actions.quote_snapshot exists but is {tuple(col)}; "
                           "expected jsonb NOT NULL DEFAULT '{}'. Fix it by hand, then re-run.")

    # The CHECK is replaced whatever version is there (table-scoped DROP IF EXISTS).
    conn.execute(text(
        f"ALTER TABLE public.contact_lookup_action_results DROP CONSTRAINT IF EXISTS {_CHECK}"
    ))
    conn.execute(text(
        f"ALTER TABLE public.contact_lookup_action_results ADD CONSTRAINT {_CHECK} "
        f"CHECK (disposition IN ({_sql_list(_DISPOSITIONS)})) NOT VALID"
    ))
    conn.execute(text(
        f"ALTER TABLE public.contact_lookup_action_results VALIDATE CONSTRAINT {_CHECK}"
    ))

    # The FK is kept only if it IS ours, checked by definition; an impostor is rebuilt.
    existing = _fk_definition(conn)
    if existing is not None and existing.replace(" NOT VALID", "") != _FK_DEF:
        conn.execute(text(f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT {_FK}"))
        existing = None
    if existing is None:
        conn.execute(text(
            f"ALTER TABLE public.pending_skip_trace_rows ADD CONSTRAINT {_FK} "
            "FOREIGN KEY (action_id, user_id) "
            "REFERENCES public.contact_lookup_actions (id, user_id) "
            "ON DELETE NO ACTION NOT VALID"
        ))
    conn.execute(text(f"ALTER TABLE public.pending_skip_trace_rows VALIDATE CONSTRAINT {_FK}"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    actions = conn.execute(text("SELECT count(*) FROM contact_lookup_actions")).scalar()
    if actions:
        raise RuntimeError(
            f"107 downgrade refused: {actions} contact-lookup action(s) exist, and dropping "
            "quote_snapshot would destroy the quote evidence they carry."
        )
    in_use = conn.execute(text(
        "SELECT count(*) FROM contact_lookup_action_results "
        "WHERE disposition = 'unmatched_unbilled'"
    )).scalar()
    if in_use:
        raise RuntimeError(
            f"107 downgrade refused: {in_use} action verdict(s) are 'unmatched_unbilled'; "
            "the 106 CHECK would reject them. Resolve them first."
        )
    conn.execute(text(f"ALTER TABLE pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {_FK}"))
    conn.execute(text(f"ALTER TABLE contact_lookup_action_results DROP CONSTRAINT IF EXISTS {_CHECK}"))
    conn.execute(text(
        f"ALTER TABLE contact_lookup_action_results ADD CONSTRAINT {_CHECK} "
        f"CHECK (disposition IN ({_sql_list(_PREVIOUS_DISPOSITIONS)}))"
    ))
    conn.execute(text("ALTER TABLE contact_lookup_actions DROP COLUMN IF EXISTS quote_snapshot"))
