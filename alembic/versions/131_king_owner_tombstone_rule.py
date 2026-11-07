"""king_parcel_owners: a privacy tombstone names the rule that withheld the owner (131).

Phase 6 P5e (plan tasks/todo-privacy-phase6-p5.md, Codex consult). A King tax lead
waiting for its owner is delivered nameless, carrying the owner's property and mailing
address. When the county answer names an owner an active privacy rule withholds, the
store records a tombstone (130's outcome 'privacy_withheld', never the name). authorize()
cannot match a row that has no name, so the readers flag every lead still waiting on
that parcel themselves, and need the rule to flag it with:

  king_parcel_owners.privacy_suppression_id  the privacy_suppressions row that withheld
                                             the owner. FK, RESTRICT (rule rows are
                                             append-only; no role may delete one).
  ck_king_parcel_owners_tombstone_rule       outcome 'privacy_withheld' <=> the id is set.

EXPAND ONLY, shipped (with the readers that understand a tombstone) before any code
writes one (P5e2). Old code never selects the column.

Lock safety: king_parcel_owners is small (one row per King parcel ever answered) and is
written by beat sweeps only. ADD COLUMN without a default is catalog-only; the FK and
CHECK are added NOT VALID (every existing row has a NULL id and no tombstone, which
satisfies both; new writes are checked). Every step is idempotent, as in 130. Grants are
table-level, so the column inherits them.

Forward-only in production. downgrade() refuses while a tombstone exists (it is what
keeps a withheld owner's name out of the shared store).

Revision ID: 131
Revises: 130
Create Date: 2026-11-07
"""
from alembic import op
from sqlalchemy import text

revision = "131"
down_revision = "130"
branch_labels = None
depends_on = None

_FK = "fk_king_parcel_owners_privacy_suppression"
_CHECK = "ck_king_parcel_owners_tombstone_rule"


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    # 130 permitted the outcome but nothing writes it before this PR's successor (P5e2),
    # so none should exist. One without a rule could never satisfy the CHECK below or
    # flag a lead: refuse, and let a human decide (Codex).
    if op.get_bind().execute(text(
            "SELECT 1 FROM public.king_parcel_owners WHERE outcome = 'privacy_withheld' "
            "LIMIT 1")).first():
        raise RuntimeError("king_parcel_owners already holds privacy_withheld rows without "
                           "a rule; resolve them before applying 131")
    op.execute(text("ALTER TABLE public.king_parcel_owners "
                    "ADD COLUMN IF NOT EXISTS privacy_suppression_id uuid"))
    op.execute(text(f"ALTER TABLE public.king_parcel_owners DROP CONSTRAINT IF EXISTS {_FK}"))
    op.execute(text(
        f"ALTER TABLE public.king_parcel_owners ADD CONSTRAINT {_FK} "
        "FOREIGN KEY (privacy_suppression_id) REFERENCES public.privacy_suppressions (id) "
        "ON DELETE RESTRICT NOT VALID"))
    op.execute(text(f"ALTER TABLE public.king_parcel_owners DROP CONSTRAINT IF EXISTS {_CHECK}"))
    op.execute(text(
        f"ALTER TABLE public.king_parcel_owners ADD CONSTRAINT {_CHECK} "
        "CHECK ((outcome = 'privacy_withheld') = (privacy_suppression_id IS NOT NULL)) "
        "NOT VALID"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    if op.get_bind().execute(text(
            "SELECT 1 FROM public.king_parcel_owners WHERE outcome = 'privacy_withheld' "
            "LIMIT 1")).first():
        raise RuntimeError("king_parcel_owners holds privacy_withheld tombstones; "
                           "refusing to downgrade 131")
    op.execute(text(f"ALTER TABLE public.king_parcel_owners DROP CONSTRAINT IF EXISTS {_CHECK}"))
    op.execute(text(f"ALTER TABLE public.king_parcel_owners DROP CONSTRAINT IF EXISTS {_FK}"))
    op.execute(text("ALTER TABLE public.king_parcel_owners DROP COLUMN IF EXISTS "
                    "privacy_suppression_id"))
