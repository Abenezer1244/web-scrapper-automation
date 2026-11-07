"""nts_notices: a notice whose grantor a privacy rule withholds remembers the rule (132).

Phase 6 P5e (plan tasks/todo-privacy-phase6-p5.md). A trustee-sale lead takes its
party_name from the notice's grantor, and the NTS matcher attaches a notice's auction
data to pre-foreclosure leads. Blanking a withheld grantor is not enough: the lead would
be built nameless, carrying the withheld person's property, and authorize() cannot match
a nameless row; and the next crawl, whose parser may not read the grantor, would switch
the notice back on. So the notice keeps the rule, and while it does it stays withheld:

  nts_notices.privacy_suppression_id  the privacy_suppressions row that withheld the
                                      grantor. FK, RESTRICT (rule rows are append-only).
  ck_nts_notices_privacy_withheld     a notice carrying a rule has NO grantor and is NOT
                                      active (so no trustee-sale lead is built from it and
                                      the live match never reads it). The crawler keeps it
                                      that way on every later crawl (P5e2); lifting a rule
                                      is P7.

EXPAND ONLY, shipped with the readers that skip such a notice (the matcher's passes and
the trustee-sale scraper) before any code writes the column (P5e2). Old code never
selects it.

Lock safety: nts_notices is a small shared cache written by beat crawls. ADD COLUMN
without a default is catalog-only; FK and CHECK are NOT VALID (every existing row has a
NULL id, which satisfies both; new writes are checked). Idempotent, as in 130/131.

Forward-only in production. downgrade() refuses while any notice carries a rule.

Revision ID: 132
Revises: 131
Create Date: 2026-11-07
"""
from alembic import op
from sqlalchemy import text

revision = "132"
down_revision = "131"
branch_labels = None
depends_on = None

_FK = "fk_nts_notices_privacy_suppression"
_CHECK = "ck_nts_notices_privacy_withheld"


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("ALTER TABLE public.nts_notices "
                    "ADD COLUMN IF NOT EXISTS privacy_suppression_id uuid"))
    op.execute(text(f"ALTER TABLE public.nts_notices DROP CONSTRAINT IF EXISTS {_FK}"))
    op.execute(text(
        f"ALTER TABLE public.nts_notices ADD CONSTRAINT {_FK} "
        "FOREIGN KEY (privacy_suppression_id) REFERENCES public.privacy_suppressions (id) "
        "ON DELETE RESTRICT NOT VALID"))
    op.execute(text(f"ALTER TABLE public.nts_notices DROP CONSTRAINT IF EXISTS {_CHECK}"))
    op.execute(text(
        f"ALTER TABLE public.nts_notices ADD CONSTRAINT {_CHECK} "
        "CHECK (privacy_suppression_id IS NULL OR (grantor IS NULL AND NOT is_active)) "
        "NOT VALID"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    if op.get_bind().execute(text(
            "SELECT 1 FROM public.nts_notices WHERE privacy_suppression_id IS NOT NULL "
            "LIMIT 1")).first():
        raise RuntimeError("nts_notices holds privacy-withheld notices; refusing to "
                           "downgrade 132")
    op.execute(text(f"ALTER TABLE public.nts_notices DROP CONSTRAINT IF EXISTS {_CHECK}"))
    op.execute(text(f"ALTER TABLE public.nts_notices DROP CONSTRAINT IF EXISTS {_FK}"))
    op.execute(text("ALTER TABLE public.nts_notices DROP COLUMN IF EXISTS "
                    "privacy_suppression_id"))
