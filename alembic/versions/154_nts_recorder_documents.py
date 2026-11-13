"""Staging for Notice-of-Trustee-Sale documents read by OCR from a recorder. (154)

Chelan publishes its NTS only as recorded documents whose sale date and amounts are
in a scanned image (no text layer). OCR can misread a digit, and a wrong auction
date is false customer data, so an OCR'd notice is STAGED here and reaches
nts_notices only after an operator compares it with the image (Codex 4a consult,
owner decision 2026-11-12).

One row per recorded document (county, transaction_item_id). The recording's own
facts (instrument, record date, document code, APN and related deed of trust from
the detail page) are kept apart from what OCR read (`fields`, each with its token
confidence and page). `ocr_text` holds the borrower's name, so only a row awaiting
review keeps it (CHECK): deciding a row, or failing to read it, clears it.

Shared, system-only table, like nts_notices (058): RLS enabled, the system role
granted in a role-existence guard (CI has no roles); FORCE is applied on prod by
scripts/apply_rls_force.sql, which lists this table.

Revision ID: 154
Revises: 153
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "154"
down_revision = "153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nts_recorder_documents",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("county", sa.String(64), nullable=False),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("transaction_item_id", sa.String(32), nullable=False),
        sa.Column("instrument_number", sa.String(32), nullable=False),
        sa.Column("doc_code", sa.String(16), nullable=False),
        sa.Column("record_date", sa.Date, nullable=False),
        sa.Column("recording_apn", sa.String(64), nullable=True),
        sa.Column("related_deed_of_trust", sa.String(32), nullable=True),
        sa.Column("pdf_sha256", sa.String(64), nullable=True),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("ocr_text", sa.Text, nullable=True),
        sa.Column("ocr_engine", sa.String(64), nullable=True),
        sa.Column("parser_version", sa.String(32), nullable=True),
        sa.Column("fields", JSONB, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending_review"),
        sa.Column("decision_note", sa.Text, nullable=True),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("nts_notice_id", UUID(as_uuid=False),
                  sa.ForeignKey("nts_notices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("county", "transaction_item_id",
                            name="uq_nts_recorder_documents_county_item"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'superseded', 'failed')",
            name="ck_nts_recorder_documents_status",
        ),
        # Only a row awaiting review keeps OCR text (it carries the borrower's name).
        sa.CheckConstraint(
            "status = 'pending_review' OR ocr_text IS NULL",
            name="ck_nts_recorder_documents_text_retention",
        ),
        # A decided row records who decided it and when.
        sa.CheckConstraint(
            "status IN ('pending_review', 'failed') OR "
            "(reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_nts_recorder_documents_decided",
        ),
    )
    op.create_index(
        "ix_nts_recorder_documents_pending", "nts_recorder_documents", ["county", "record_date"],
        postgresql_where=sa.text("status = 'pending_review'"),
    )

    op.execute("ALTER TABLE nts_recorder_documents ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $nrd_rls$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON nts_recorder_documents
                    TO bridgeleads_system;
                DROP POLICY IF EXISTS nts_recorder_documents_system ON nts_recorder_documents;
                CREATE POLICY nts_recorder_documents_system ON nts_recorder_documents
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $nrd_rls$;
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS nts_recorder_documents_system ON nts_recorder_documents")
    op.drop_index("ix_nts_recorder_documents_pending", table_name="nts_recorder_documents")
    op.drop_table("nts_recorder_documents")
