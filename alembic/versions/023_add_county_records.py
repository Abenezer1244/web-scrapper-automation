"""Add county_records and user_record_views tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())

    if "county_records" not in existing_tables:
        op.create_table(
            "county_records",
            sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("county", sa.String(64), nullable=False),
            sa.Column("state", sa.String(4), nullable=False),
            sa.Column("doc_type", sa.String(128), nullable=True),
            sa.Column("date_recorded", sa.String(32), nullable=True),
            sa.Column("party_name", sa.String(512), nullable=True),
            sa.Column("heirs", sa.Text, nullable=True),
            sa.Column("legal_description", sa.Text, nullable=True),
            sa.Column("parcel_id", sa.String(64), nullable=True),
            sa.Column("property_address", sa.String(512), nullable=True),
            sa.Column("mailing_address", sa.String(512), nullable=True),
            sa.Column("enrichment_data", sa.JSON, nullable=True),
            sa.Column("record_hash", sa.String(32), nullable=False, unique=True),
            sa.Column("scraped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("batch_date", sa.Date, server_default=sa.text("CURRENT_DATE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    op.execute("CREATE INDEX IF NOT EXISTS idx_county_records_county_state_scraped ON county_records (county, state, scraped_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_county_records_batch_date ON county_records (batch_date)")
    # Note: record_hash is declared unique=True on the table, which already
    # creates a unique btree (county_records_record_hash_key). We deliberately
    # do NOT add a second `idx_county_records_hash` here — every new daily
    # batch INSERT ... ON CONFLICT (record_hash) would otherwise have to
    # maintain two equivalent indexes for the same column.

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX IF NOT EXISTS idx_county_records_party_trgm ON county_records USING gin (party_name gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_county_records_address_trgm ON county_records USING gin (property_address gin_trgm_ops)")

    # county_records is a shared dedup cache keyed by record_hash (no user_id
    # column). Reads are intentionally cross-tenant: the cache is global by
    # design. Writes must come from system-context paths only (the worker /
    # scheduler / canary tasks that legitimately mutate cross-tenant tables).
    # API request paths set app.current_user_id (via rls_sync_session(user_id)
    # or get_rls_db) and must NOT write to this table.
    #
    # Enforcement is a row-level trigger, NOT RLS. The repo's production DB
    # role currently has BYPASSRLS (see src/db/session.py:check_rls_role_status)
    # which makes RLS policies advisory rather than enforced. Triggers fire
    # regardless of role privileges — including for BYPASSRLS and superuser
    # roles — so the trigger is the actual guarantee. We deliberately do NOT
    # install RLS policies here: under the current role they would be no-ops,
    # and pretending they provide layered defense would only hide the fact
    # that the trigger is the sole enforcement layer. If the BYPASSRLS role
    # downgrade ever ships, RLS policies for this table can be added in a
    # follow-up migration alongside any other tenant-isolation work that
    # downgrade requires (e.g., for county_connectors, etc.) — that work is
    # out of scope here.
    op.execute("CREATE OR REPLACE FUNCTION county_records_block_user_writes() "
               "RETURNS trigger AS $$ "
               "DECLARE uid TEXT; "
               "BEGIN "
               "    uid := COALESCE(NULLIF(current_setting('app.current_user_id', true), ''), ''); "
               "    IF uid <> '' THEN "
               "        RAISE EXCEPTION 'county_records mutations are not permitted from user-scoped sessions (app.current_user_id=%). Use SyncSessionLocal() / system_sync_session() without setting RLS context.', uid "
               "        USING ERRCODE = 'insufficient_privilege'; "
               "    END IF; "
               "    RETURN COALESCE(NEW, OLD); "
               "END; "
               "$$ LANGUAGE plpgsql;")
    op.execute("DROP TRIGGER IF EXISTS county_records_block_user_writes_trg ON county_records")
    op.execute("CREATE TRIGGER county_records_block_user_writes_trg "
               "BEFORE INSERT OR UPDATE OR DELETE ON county_records "
               "FOR EACH ROW EXECUTE FUNCTION county_records_block_user_writes();")
    # Clean up any RLS policies left over from earlier drafts of this
    # migration before disabling RLS, then disable RLS so the trigger is
    # the unambiguous enforcement layer.
    op.execute("DROP POLICY IF EXISTS county_records_read ON county_records")
    op.execute("DROP POLICY IF EXISTS county_records_write ON county_records")
    op.execute("DROP POLICY IF EXISTS county_records_update ON county_records")
    op.execute("DROP POLICY IF EXISTS county_records_delete ON county_records")
    op.execute("ALTER TABLE county_records DISABLE ROW LEVEL SECURITY")

    if "user_record_views" not in existing_tables:
        op.create_table(
            "user_record_views",
            sa.Column("id", UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("scraper_config_id", UUID(as_uuid=False), sa.ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("last_viewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("user_id", "scraper_config_id", name="uq_user_config_view"),
        )

    # Use NULLIF(...)::uuid to match the crash-safe pattern from migration
    # 018: when app.current_user_id is unset, current_setting(..., true)
    # returns '' which cannot be cast to uuid and would crash queries.
    op.execute("ALTER TABLE user_record_views ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS urv_user_only ON user_record_views")
    op.execute(
        "CREATE POLICY urv_user_only ON user_record_views FOR ALL "
        "USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS urv_user_only ON user_record_views")
    op.execute("DROP TABLE IF EXISTS user_record_views")
    op.execute("DROP TRIGGER IF EXISTS county_records_block_user_writes_trg ON county_records")
    op.execute("DROP FUNCTION IF EXISTS county_records_block_user_writes()")
    op.execute("DROP TABLE IF EXISTS county_records")
