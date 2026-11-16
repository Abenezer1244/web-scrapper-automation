"""Offer death_certificate on the Cowlitz recorder connector (owner-approved 2026-11-15).

WHY: Cowlitz's Laserfiche WebLink recorder indexes "Death Certificate" and "Death
Certificate to Perfect Death Deed" (2 in 10/15-11/14/2026). They were collected as
probate; the template now gives them their own type, as King/Chelan/Clark define it
(publicly RECORDED death certificates only, never a vital-records order). The decedent
is promoted from the grantee when the State is the grantor.

The connector row is active, so this type is offered as soon as this runs: it runs
only with an owner-approved deploy. Idempotent: the WHERE guard skips a row that
already offers it (same shape as 157).

Revision ID: 166
Revises: 165
Create Date: 2026-11-15
"""

from alembic import op

revision = "166"
down_revision = "165"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE county_connectors
        SET record_types = (record_types::jsonb || '["death_certificate"]'::jsonb)::json
        WHERE lower(county) = 'cowlitz'
          AND lower(state) = 'wa'
          AND NOT (record_types::jsonb ? 'death_certificate')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE county_connectors
        SET record_types = (
            SELECT COALESCE(jsonb_agg(elem ORDER BY ord), '[]'::jsonb)::json
            FROM jsonb_array_elements_text(record_types::jsonb)
                 WITH ORDINALITY AS t(elem, ord)
            WHERE elem <> 'death_certificate'
        )
        WHERE lower(county) = 'cowlitz'
          AND lower(state) = 'wa'
        """
    )
