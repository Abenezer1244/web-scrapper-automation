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
   to find. Both run in the migration's single transaction, so the ADD's SHARE ROW
   EXCLUSIVE lock (it blocks the queue's writers) is held through that scan. The
   table is small (~1k rows in production), so that is a brief block, and
   lock_timeout bounds the wait to acquire it.

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

Lock safety: every step runs under `lock_timeout = 5s`, so a boot migration behind a
long reader fails fast instead of queueing the queue's writers behind it.

Forward-only in production. downgrade() exists for test databases; it refuses to run
while any row already uses `unmatched_unbilled`, rather than silently rewrite it.

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


def _has_constraint(conn, name: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}
    ).scalar() is not None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))

    conn.execute(text(
        "ALTER TABLE contact_lookup_actions "
        "ADD COLUMN IF NOT EXISTS quote_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb"
    ))

    # Replay-safe: drop whichever version of the CHECK is there, add the current one.
    conn.execute(text(f"ALTER TABLE contact_lookup_action_results DROP CONSTRAINT IF EXISTS {_CHECK}"))
    conn.execute(text(
        f"ALTER TABLE contact_lookup_action_results ADD CONSTRAINT {_CHECK} "
        f"CHECK (disposition IN ({_sql_list(_DISPOSITIONS)})) NOT VALID"
    ))
    conn.execute(text(f"ALTER TABLE contact_lookup_action_results VALIDATE CONSTRAINT {_CHECK}"))

    if not _has_constraint(conn, _FK):
        conn.execute(text(
            f"ALTER TABLE pending_skip_trace_rows ADD CONSTRAINT {_FK} "
            "FOREIGN KEY (action_id, user_id) "
            "REFERENCES contact_lookup_actions (id, user_id) "
            "ON DELETE NO ACTION NOT VALID"
        ))
    conn.execute(text(f"ALTER TABLE pending_skip_trace_rows VALIDATE CONSTRAINT {_FK}"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
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
