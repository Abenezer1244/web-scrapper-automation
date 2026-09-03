"""results.property_city / property_zip — structured SITUS location beside the frozen street key.

"Real data everywhere" (2026-09-02): property_address is the assessor's street-only
situs line and is FROZEN as the dedup/billing key and the skip-trace key, so the
city/state/zip of the property could never be stored — property_state was always
NULL, out_of_state_owner could never be computed, and absentee_owner could never be
a confirmed False (same street, nothing to confirm the place). These two nullable
columns (plus the repurposed property_state) hold the situs parts from a REAL
source only: the notice's "commonly known as" line, the statewide parcel layer's
SITUS_CITY_NM / SITUS_ZIP_NR, or a county row asserting mail goes to the property.
Nothing is inferred; a lead with no source for them keeps NULL and unknown flags.

No data change here — the backfill script fills them from sources with evidence.
Nullable ADD COLUMN only (no rewrite, no lock beyond the catalog change).

Revision ID: 085
Revises: 084
"""
import sqlalchemy as sa
from alembic import op

revision = "085"
down_revision = "084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("property_city", sa.String(128), nullable=True))
    op.add_column("results", sa.Column("property_zip", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("results", "property_zip")
    op.drop_column("results", "property_city")
