r"""Add results.date_recorded_parsed generated filing-date column. (049)

The date-windowed overlap/combine feature (Lists page + batch scrape) filters
leads by their COUNTY FILING DATE, stored as free-form text in
results.date_recorded. A read-only spike over the full prod table (293,451 rows)
found that field effectively clean: 0.0% null, 100% parseable, ONE format family
— US M/D/YYYY, slash-separated, single- or double-digit month/day.

We materialize it into a GENERATED ALWAYS ... STORED DATE column so existing AND
future rows are filled automatically (no app parsing, no backfill) and the value
is indexable.

IMMUTABILITY (Codex P1): a generated-column expression MUST be IMMUTABLE.
`to_date(text,text)` is only STABLE, so it cannot be used here — the ALTER would
fail. We instead use an IMMUTABLE PL/pgSQL helper `result_parse_filing_date`:
  - regex-guards the shape (non-`M/D/YYYY`/empty/garbage -> NULL),
  - builds the date with `make_date` (IMMUTABLE),
  - EXCEPTION-traps invalid calendar dates (e.g. '02/30/2024') -> NULL, so a
    single bad row can never break the column backfill or future INSERTs.
`make_date`, `split_part`, `btrim`, and text->int cast are all immutable; the
function does no table/config access, so the IMMUTABLE label is honest.

Partial index excludes NULL property_key rows (overlap is strong-identity only).

Revision ID: 049
Revises: 048
Create Date: 2026-06-10
"""
from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None

# Keep this function body IDENTICAL to the DDL registered in src/db/models.py
# (before_create event) so prod (migration) and test create_all emit the same fn.
_PARSE_FN = r"""
CREATE OR REPLACE FUNCTION result_parse_filing_date(txt text)
RETURNS date
LANGUAGE plpgsql
IMMUTABLE
AS $func$
BEGIN
    IF txt IS NULL OR btrim(txt) !~ '^\d{1,2}/\d{1,2}/\d{4}$' THEN
        RETURN NULL;
    END IF;
    RETURN make_date(
        split_part(btrim(txt), '/', 3)::int,
        split_part(btrim(txt), '/', 1)::int,
        split_part(btrim(txt), '/', 2)::int
    );
EXCEPTION WHEN data_exception THEN
    -- SQLSTATE class 22 (invalid datetime, field overflow, bad cast). Covers
    -- every way a malformed date can fail here -> NULL, without masking
    -- non-data errors (Codex P2). A generated column must never raise.
    RETURN NULL;
END;
$func$;
"""


def upgrade() -> None:
    op.execute(_PARSE_FN)
    op.execute(
        "ALTER TABLE results ADD COLUMN date_recorded_parsed DATE "
        "GENERATED ALWAYS AS (result_parse_filing_date(date_recorded)) STORED"
    )
    op.execute(
        "CREATE INDEX ix_results_user_filingdate ON results "
        "(user_id, date_recorded_parsed) WHERE property_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_results_user_filingdate")
    op.execute("ALTER TABLE results DROP COLUMN IF EXISTS date_recorded_parsed")
    op.execute("DROP FUNCTION IF EXISTS result_parse_filing_date(text)")
