"""results: the privacy rule generation a row was last checked against; King owner store:
a privacy tombstone outcome (130).

Phase 6 P5 (docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md,
section 6, "an enforcer sweep re-applies rules so stored state converges"; plan
tasks/todo-privacy-phase6-p5.md).

  results.privacy_checked_gen   the rule GENERATION this row was last authorized
                                against. The generation is the number of 'active'
                                rule rows ever written (privacy_suppressions is
                                append-only, so it only grows, and only activation
                                grows it). The enforcer sweep reads rows whose
                                generation is behind the current one, so a full pass
                                runs once per activation, never once per tick (Codex:
                                results has no updated_at to sweep by). 0 = never
                                checked; no rule has ever been active, so 0 is
                                current today and the sweep has nothing to do.
  ix_results_privacy_unchecked  (privacy_checked_gen, id) over unflagged rows: the
                                sweep's keyset scan.
  king_parcel_owners.outcome    gains 'privacy_withheld': a parcel whose owner a rule
                                withholds is recorded WITHOUT the name, so the shared
                                store never holds it and the county is not queried
                                again for it on every pass. The existing pairing CHECK
                                (outcome 'found' <=> a name) already forbids a name on
                                it. Clearing the tombstone when a rule is lifted is P7.

EXPAND ONLY, shipped alone before any code that reads or writes the column or the
outcome (Railway migrates at API boot while workers start; see 126). Old code
ignores both.

Lock safety: ADD COLUMN with a constant default is catalog-only (PostgreSQL 11+). The
widened CHECK is added NOT VALID (every existing outcome is in the old, narrower set,
so nothing is lost; new writes are still checked). The index is built CONCURRENTLY in
an autocommit block after that transaction commits, dropping a dead INVALID leftover
first (the 096 restart pattern; migrations are serialized by scripts/migrate.py's
advisory lock, so an invalid index here is dead, not building). Grants: results and
king_parcel_owners grants are table-level, so the column inherits them.

Forward-only in production: rolling back is a code revert. downgrade() exists for test
databases.

Revision ID: 130
Revises: 129
Create Date: 2026-11-07
"""
from alembic import op
from sqlalchemy import text

revision = "130"
down_revision = "129"
branch_labels = None
depends_on = None

_INDEX = "ix_results_privacy_unchecked"
_CREATE = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
    "ON public.results (privacy_checked_gen, id) "
    "WHERE privacy_suppressed_at IS NULL"
)
_INVALID_CHECK = text(
    "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname = :name AND NOT i.indisvalid"
)
_OUTCOME_CHECK = "ck_king_parcel_owners_outcome"


def upgrade() -> None:
    # Every step before the index is idempotent: autocommit_block() commits them, so a
    # failed index build leaves them applied with 130 unrecorded, and the retry re-runs
    # them (Codex).
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("ALTER TABLE public.results ADD COLUMN IF NOT EXISTS "
                    "privacy_checked_gen bigint NOT NULL DEFAULT 0"))
    op.execute(text(
        f"ALTER TABLE public.king_parcel_owners DROP CONSTRAINT IF EXISTS {_OUTCOME_CHECK}"))
    op.execute(text(
        f"ALTER TABLE public.king_parcel_owners ADD CONSTRAINT {_OUTCOME_CHECK} "
        "CHECK (outcome IN ('found', 'not_on_record', 'privacy_withheld')) NOT VALID"
    ))
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            if conn.execute(_INVALID_CHECK, {"name": _INDEX}).first():
                conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
            conn.execute(text(_CREATE))
        finally:
            conn.execute(text("RESET lock_timeout"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.get_bind().execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    # Never silently delete a tombstone: it is what keeps a withheld owner's name out
    # of the shared store. A database holding one must be cleaned by a human first.
    if op.get_bind().execute(text(
            "SELECT 1 FROM public.king_parcel_owners WHERE outcome = 'privacy_withheld' "
            "LIMIT 1")).first():
        raise RuntimeError("king_parcel_owners holds privacy_withheld tombstones; "
                           "refusing to downgrade 130")
    op.execute(text(f"ALTER TABLE public.king_parcel_owners DROP CONSTRAINT {_OUTCOME_CHECK}"))
    op.execute(text(
        f"ALTER TABLE public.king_parcel_owners ADD CONSTRAINT {_OUTCOME_CHECK} "
        "CHECK (outcome IN ('found', 'not_on_record'))"
    ))
    op.drop_column("results", "privacy_checked_gen")
