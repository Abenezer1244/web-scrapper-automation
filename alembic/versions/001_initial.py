"""Initial schema — all tables, RLS policies, first county connector

Revision ID: 001
Revises:
Create Date: 2026-03-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=True),
        sa.Column("plan", sa.String(32), nullable=False, server_default="starter"),
        sa.Column("records_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_limit", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_api_key_hash", "users", ["api_key_hash"])

    # ─── scraper_configs ──────────────────────────────────────────────────────
    op.create_table(
        "scraper_configs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("county", sa.String(128), nullable=False),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("record_type", sa.String(64), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("enrichment", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("schedule", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("deliver", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_scraper_configs_user_id", "scraper_configs", ["user_id"])

    # ─── jobs ─────────────────────────────────────────────────────────────────
    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scraper_config_id", UUID(as_uuid=False), sa.ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("trigger", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("page_current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("export_key", sa.String(512), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    op.create_index("ix_jobs_scraper_config_id", "jobs", ["scraper_config_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    # ─── results ──────────────────────────────────────────────────────────────
    op.create_table(
        "results",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=False), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date_recorded", sa.String(32), nullable=True),
        sa.Column("party_name", sa.String(512), nullable=True),
        sa.Column("heirs", sa.Text(), nullable=True),
        sa.Column("legal_description", sa.Text(), nullable=True),
        sa.Column("parcel_id", sa.String(64), nullable=True),
        sa.Column("property_address", sa.String(512), nullable=True),
        sa.Column("mailing_address", sa.String(512), nullable=True),
        sa.Column("enrichment_data", sa.JSON(), nullable=True, server_default="{}"),
        sa.Column("raw_html_hash", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_results_job_id", "results", ["job_id"])
    op.create_index("ix_results_user_id", "results", ["user_id"])
    op.create_index("ix_results_raw_html_hash", "results", ["raw_html_hash"])

    # ─── county_connectors ────────────────────────────────────────────────────
    op.create_table(
        "county_connectors",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("county", sa.String(128), nullable=False),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("record_types", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("scraper_class", sa.String(255), nullable=False),
        sa.Column("render_mode", sa.String(16), nullable=False, server_default="playwright"),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("health_status", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("last_checked", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_county_connectors_county", "county_connectors", ["county"])
    op.create_index("ix_county_connectors_state", "county_connectors", ["state"])

    # ─── job_logs ─────────────────────────────────────────────────────────────
    op.create_table(
        "job_logs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=False), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("level", sa.String(16), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_job_logs_job_id", "job_logs", ["job_id"])

    # ─── Row Level Security ───────────────────────────────────────────────────
    # Enable RLS — users can only see their own data
    op.execute("ALTER TABLE scraper_configs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE results ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE job_logs ENABLE ROW LEVEL SECURITY")

    # RLS policies — app sets app.current_user_id via SET LOCAL
    op.execute("""
        CREATE POLICY scraper_configs_user_isolation ON scraper_configs
        USING (user_id = current_setting('app.current_user_id', true)::uuid)
    """)
    op.execute("""
        CREATE POLICY jobs_user_isolation ON jobs
        USING (user_id = current_setting('app.current_user_id', true)::uuid)
    """)
    op.execute("""
        CREATE POLICY results_user_isolation ON results
        USING (user_id = current_setting('app.current_user_id', true)::uuid)
    """)
    op.execute("""
        CREATE POLICY job_logs_via_job ON job_logs
        USING (
            job_id IN (
                SELECT id FROM jobs
                WHERE user_id = current_setting('app.current_user_id', true)::uuid
            )
        )
    """)

    # ─── Seed: Pierce County, WA — Probate connector ─────────────────────────
    import uuid as _uuid
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'pierce',
            'wa',
            '["probate"]',
            'src.scrapers.pierce_wa_probate.PierceWAProbateScraper',
            'playwright',
            'https://armsweb.co.pierce.wa.us/SearchEntry.aspx',
            'unknown',
            true
        )
    """)


def downgrade() -> None:
    # Drop RLS policies first
    op.execute("DROP POLICY IF EXISTS scraper_configs_user_isolation ON scraper_configs")
    op.execute("DROP POLICY IF EXISTS jobs_user_isolation ON jobs")
    op.execute("DROP POLICY IF EXISTS results_user_isolation ON results")
    op.execute("DROP POLICY IF EXISTS job_logs_via_job ON job_logs")

    op.drop_table("job_logs")
    op.drop_table("results")
    op.drop_table("jobs")
    op.drop_table("scraper_configs")
    op.drop_table("county_connectors")
    op.drop_table("users")
