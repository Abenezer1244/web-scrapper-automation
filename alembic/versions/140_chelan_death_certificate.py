"""Offer death_certificate on the Chelan connector (owner-approved 2026-11-12).

WHY: Chelan's recorder indexes DEATH CERTIFICATE as the code "DC". The Chelan
AcclaimWeb scraper collects it as King defines the type (recorded death
certificates only) and names the decedent (ChelanAcclaimWebScraper, commit 9d9a6d8f).
A death certificate's recording names no parcel, so a lead gets an address only
through the post-save owner-name fallback; leads without one are not delivered.

This only appends to the Chelan connector's record_types (a JSON array, edited as
066 does). Idempotent: the WHERE guard skips a row that already offers it.

Revision ID: 140
Revises: 139
Create Date: 2026-11-12
"""

from alembic import op

revision = "140"
down_revision = "139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE county_connectors
        SET record_types = (record_types::jsonb || '["death_certificate"]'::jsonb)::json
        WHERE lower(county) = 'chelan'
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
        WHERE lower(county) = 'chelan'
          AND lower(state) = 'wa'
        """
    )
