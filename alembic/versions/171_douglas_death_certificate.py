"""Offer death_certificate on the Douglas connector (owner-approved 2026-11-15).

WHY: Douglas's recorder indexes recorded death certificates as DEATH CERTIFICATE
(21 in the 12 months to 2026-11-15, read live). DouglasAcclaimWebScraper collects
them as King and Chelan define the type and names the decedent; 6 of the 21 carry
the recording's own 11-digit parcel, the rest get an address only through the
existing post-save owner lookups, and leads without one are not delivered.

This only appends to the Douglas connector's record_types (a JSON array, edited as
157 does for Chelan). Idempotent: the WHERE guard skips a row that already offers it.

Revision ID: 171
Revises: 170
Create Date: 2026-11-15
"""

from alembic import op

revision = "171"
down_revision = "170"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE county_connectors
        SET record_types = (record_types::jsonb || '["death_certificate"]'::jsonb)::json
        WHERE lower(county) = 'douglas'
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
        WHERE lower(county) = 'douglas'
          AND lower(state) = 'wa'
        """
    )
