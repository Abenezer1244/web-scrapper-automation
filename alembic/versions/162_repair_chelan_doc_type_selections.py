"""Repair Chelan pre-foreclosure selections left stale by #625.

#625 narrowed Chelan's selectable pre-foreclosure document types to Notice of
Trustee Sale and Lis Pendens: Chelan's "NOD" is a Notice of Discontinuance, and it
records no Notice of Default or Foreclosure under those names. A Chelan scraper saved
earlier with the old four types then failed every run, because the strict mapping
refuses a type it cannot search rather than quietly searching everything.

This drops the types Chelan no longer offers from each such saved selection and keeps
the rest in the customer's order. It never adds a type. A selection with nothing
valid left is left untouched, so the run keeps failing with a message that tells the
customer what changed (StaleDocTypeSelectionError).

FROZEN on purpose: the allowed set is written out here, not read from
src/scrapers/doc_types.py, so replaying this migration later does what it did today.
Every repaired row gets an audit_events row ('scraper_doc_types_repaired', path
'alembic:162') with the old and new selection, attributed to the scraper's owner and
marked as a system repair. One statement, so a customer's concurrent edit cannot be
overwritten by a stale read. updated_at advances, so an Edit page opened before the
repair cannot save over it unseen.

Downgrade is a no-op: the dropped types are not searchable in Chelan, so restoring
them would only restore the failure. The audit rows keep the original selections.

Revision ID: 162
Revises: 161
Create Date: 2026-11-15
"""

import logging

from alembic import op
from sqlalchemy import text

revision = "162"
down_revision = "161"
branch_labels = None
depends_on = None

_log = logging.getLogger("alembic.runtime.migration")

# Chelan's selectable types as of #625 (src/scrapers/doc_types.py, ("chelan", "wa")).
_ALLOWED = ("notice_of_trustee_sale", "lis_pendens")

_SCOPE = """
    lower(sc.county) = 'chelan' AND upper(sc.state) = 'WA'
    AND sc.record_type = 'pre_foreclosure'
    AND sc.doc_types IS NOT NULL AND json_typeof(sc.doc_types) = 'array'
"""


def upgrade() -> None:
    bind = op.get_bind()
    allowed = list(_ALLOWED)
    repaired = bind.execute(
        text(f"""
            WITH cand AS (
                SELECT sc.id, sc.user_id, sc.doc_types::text AS old,
                       (SELECT json_agg(e.v ORDER BY e.o)
                          FROM json_array_elements_text(sc.doc_types) WITH ORDINALITY AS e(v, o)
                         WHERE e.v = ANY(:allowed)) AS kept,
                       (SELECT count(*)
                          FROM json_array_elements_text(sc.doc_types) AS e(v)
                         WHERE e.v IS NULL OR NOT (e.v = ANY(:allowed))) AS dropped
                  FROM scraper_configs sc
                 WHERE {_SCOPE}
                   FOR UPDATE
            ),
            upd AS (
                UPDATE scraper_configs sc
                   SET doc_types = c.kept, updated_at = now()
                  FROM cand c
                 WHERE sc.id = c.id AND c.dropped > 0 AND c.kept IS NOT NULL
             RETURNING sc.id, sc.user_id, c.old, c.kept::text AS new
            ),
            aud AS (
                INSERT INTO audit_events (id, event, user_id, ip, path, detail, created_at)
                SELECT gen_random_uuid(), 'scraper_doc_types_repaired', upd.user_id, NULL,
                       'alembic:162',
                       left('config_id=' || upd.id || ' system_repair reason=chelan_types_narrowed_625 '
                            || 'doc_types=' || upd.old || '->' || upd.new, 512),
                       now()
                  FROM upd
             RETURNING 1
            )
            SELECT (SELECT count(*) FROM upd) AS repaired, (SELECT count(*) FROM aud) AS audited
        """),
        {"allowed": allowed},
    ).one()
    left_stale = bind.execute(
        text(f"""
            SELECT count(*) FROM scraper_configs sc
             WHERE {_SCOPE}
               AND EXISTS (SELECT 1 FROM json_array_elements_text(sc.doc_types) AS e(v)
                            WHERE e.v IS NULL OR NOT (e.v = ANY(:allowed)))
        """),
        {"allowed": allowed},
    ).scalar_one()
    _log.info(
        "162: repaired %d Chelan pre-foreclosure selection(s) (%d audited); %d left as is "
        "with nothing valid to keep", repaired.repaired, repaired.audited, left_stale,
    )


def downgrade() -> None:
    pass
