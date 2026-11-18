"""nts_recorder_documents.entry_name: a recording's stable name in a recorder whose
document ids are not stable, and the five dead Cowlitz discontinuance twins removed.

Cowlitz's Laserfiche repository re-indexed overnight on 2026-11-16/17: every entry id
moved by about +9963 and the old ids now open an error page. Staging is unique on
(county, transaction_item_id), so the next crawl staged all 20 recordings again. The 14
dead notice twins were rejected (duplicate_recording) by the operator CLI. This adds
the entry's full name ("3799745-0"; a second entry under one recording is "-1"), unique
per county where known; the crawl matches on it and moves the row to the live id
(nts_crawler.relocate_recorder_entries). Chelan carries no entry name: untouched.

The five dead discontinuance twins cannot be rejected (a discontinuance is resolved,
not rejected), and approval takes the first discontinuance that matches, so a twin
makes that choice ambiguous (Codex). Exactly these five rows are deleted, each only if
its live twin (named here) is the same recording with the same evidence and the dead
row recorded no cancellation and no review. On any other database nothing matches.

Revision ID: 170
Revises: 169
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "170"
down_revision = "169"
branch_labels = None
depends_on = None

# (dead twin, live twin), verified 2026-11-17: same instrument, entry id +9962..+9964.
_NOD_TWINS = (
    ("fcb60f95-99bc-46d2-b3fd-233adcd957fa", "a99514ed-1022-4c59-a9ec-0f8395838116"),  # 3798818
    ("4a3d10ae-7fce-4018-9972-b57fb3842e62", "506945dc-8f0e-4052-a5d9-cfc237dc8147"),  # 3799351
    ("a26238cc-e692-4644-92ee-2a50631c7901", "c7754753-7d9d-49dd-b752-7c34ee220137"),  # 3799387
    ("5563d898-5601-4d46-b5ec-3efca0501909", "f7c02be6-2fd3-4a88-93ef-0641096a1cb2"),  # 3799388
    ("244e470e-18f9-4878-8a3a-9ae0790f748e", "fb35e4be-aa4f-4903-a8ba-0b08a4952635"),  # 3799483
)


def upgrade() -> None:
    op.add_column("nts_recorder_documents", sa.Column("entry_name", sa.String(64), nullable=True))
    op.create_index("uq_nts_recorder_documents_county_entry", "nts_recorder_documents",
                    ["county", "entry_name"], unique=True,
                    postgresql_where=sa.text("entry_name IS NOT NULL"))
    for dead, live in _NOD_TWINS:
        op.execute(sa.text("""
            DELETE FROM nts_recorder_documents d
             USING nts_recorder_documents l
             WHERE d.id = CAST(:dead AS uuid) AND l.id = CAST(:live AS uuid)
               AND d.county = 'cowlitz' AND l.county = 'cowlitz'
               AND d.doc_code = 'NOD' AND l.doc_code = 'NOD'
               AND d.status = 'discontinuance' AND l.status = 'discontinuance'
               AND d.instrument_number = l.instrument_number
               AND d.record_date = l.record_date
               AND d.created_at < l.created_at
               AND d.reviewed_by IS NULL AND d.reviewed_at IS NULL
               AND COALESCE(jsonb_array_length(d.fields -> 'cancelled'), 0) = 0
               AND d.fields -> 'related' = l.fields -> 'related'
               AND d.fields -> 'recording_apns' = l.fields -> 'recording_apns'
        """).bindparams(dead=dead, live=live))


def downgrade() -> None:
    # The deleted twins pointed at ids the recorder no longer serves: not restored.
    op.drop_index("uq_nts_recorder_documents_county_entry", table_name="nts_recorder_documents")
    op.drop_column("nts_recorder_documents", "entry_name")
