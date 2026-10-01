"""AI-mode removal, Phase 2c: retire scraper_mode 'ai' for good (109).

2b (migration 108, #409) moved every 'ai' row to 'template' and made every writer store
'template'; until now the readers still accepted 'ai' so a lagging 2a process could not
break a run. This is the last step:

1. a straggler sweep: any 'ai' row an old API instance wrote during 2b's rolling deploy
   becomes 'template';
2. `ck_county_connectors_scraper_mode`: CHECK (scraper_mode IN ('template','manual')), so
   'ai' (or any unknown mode) can never be stored again. The column is NOT NULL since
   002, verified below, so the CHECK's NULL pass-through cannot let a NULL in.

MERGE GATE (tasks/todo-remove-ai-mode.md, Phase 2c): not before 2026-10-08, zero 'ai'
rows checked daily for 7 days, zero "legacy scraper_mode 'ai' normalized" log lines,
and every api/worker/beat instance on #409 or later. 2b never writes 'ai', so a 2b
process still running when this lands is unaffected.

One transaction. The table is locked SHARE ROW EXCLUSIVE first (writers wait; readers
do not), so the count, the sweep and the validation all see the same rows.
`lock_timeout = 5s` bounds only the wait to acquire it: a boot behind a long writer
fails fast and retries. ~51 rows, so ADD + VALIDATE is a brief block.

Replay-safe: an existing constraint of this name ON THIS TABLE is accepted only if it
is a CHECK with exactly the expected definition; anything else aborts for a human to
look at (never silently dropped and rebuilt).

Rollback: a revert PR that KEEPS this file (an image whose alembic graph ends at 108
cannot boot against a database at 109). Leaving the constraint in place is safe for
the reverted 2b code, which only ever writes 'template'. downgrade() exists for test
databases and drops only the constraint.

Revision ID: 109
Revises: 108
Create Date: 2026-10-01
"""
from alembic import op
from sqlalchemy import text

revision = "109"
down_revision = "108"
branch_labels = None
depends_on = None

_CHECK = "ck_county_connectors_scraper_mode"
# What pg_get_constraintdef prints for the constraint below (Postgres normalizes the IN
# list to = ANY (ARRAY[...]); " NOT VALID" is stripped before comparing).
_CHECK_DEF = ("CHECK (((scraper_mode)::text = ANY "
              "((ARRAY['template'::character varying, 'manual'::character varying])::text[])))")


def _existing(conn):
    """(contype, definition) of the constraint named _CHECK on county_connectors, or None."""
    return conn.execute(text(
        "SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = :n AND conrelid = 'public.county_connectors'::regclass"
    ), {"n": _CHECK}).one_or_none()


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text("LOCK TABLE public.county_connectors IN SHARE ROW EXCLUSIVE MODE"))

    nullable = conn.execute(text(
        "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'county_connectors' AND column_name = 'scraper_mode'"
    )).scalar()
    if nullable != "NO":
        raise RuntimeError(f"109: county_connectors.scraper_mode is_nullable={nullable!r}, expected 'NO'")

    before = conn.execute(text(
        "SELECT scraper_mode, count(*) FROM public.county_connectors "
        "GROUP BY scraper_mode ORDER BY scraper_mode"
    )).all()
    print(f"109: county_connectors by scraper_mode before: {dict(before)}", flush=True)

    swept = conn.execute(text(
        "UPDATE public.county_connectors SET scraper_mode = 'template' "
        "WHERE scraper_mode = 'ai'"
    )).rowcount
    print(f"109: swept {swept} straggler 'ai' row(s) to 'template'", flush=True)

    unknown = conn.execute(text(
        "SELECT scraper_mode, count(*) FROM public.county_connectors "
        "WHERE scraper_mode IS NULL OR scraper_mode NOT IN ('template', 'manual') "
        "GROUP BY scraper_mode"
    )).all()
    if unknown:
        raise RuntimeError(
            f"109: unknown scraper_mode value(s) {dict(unknown)}; expected only "
            f"('template', 'manual'). Fix the rows, then re-run."
        )

    existing = _existing(conn)
    if existing is None:
        conn.execute(text(
            f"ALTER TABLE public.county_connectors ADD CONSTRAINT {_CHECK} "
            "CHECK (scraper_mode IN ('template', 'manual')) NOT VALID"
        ))
    elif existing.contype != "c" or existing[1].replace(" NOT VALID", "") != _CHECK_DEF:
        raise RuntimeError(
            f"109: a constraint named {_CHECK} already exists on county_connectors but is "
            f"not the expected CHECK ({existing.contype!r}: {existing[1]!r}). Inspect it by hand."
        )
    conn.execute(text(f"ALTER TABLE public.county_connectors VALIDATE CONSTRAINT {_CHECK}"))

    final = _existing(conn)
    validated = conn.execute(text(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conname = :n AND conrelid = 'public.county_connectors'::regclass"
    ), {"n": _CHECK}).scalar()
    if final is None or final[1] != _CHECK_DEF or validated is not True:
        raise RuntimeError(f"109: {_CHECK} is not in place and validated: {final!r} {validated!r}")


def downgrade() -> None:
    """Test databases only: drop the CHECK. Rows stay 'template' (what 2b reads)."""
    conn = op.get_bind()
    conn.execute(text(f"ALTER TABLE public.county_connectors DROP CONSTRAINT IF EXISTS {_CHECK}"))
