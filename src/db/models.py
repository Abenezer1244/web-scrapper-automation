import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    api_key_hash = Column(String(64), nullable=True, index=True)
    plan = Column(String(32), nullable=False, default="starter")
    records_used = Column(Integer, nullable=False, default=0)
    records_limit = Column(Integer, nullable=False, default=50)
    stripe_customer_id = Column(String(64), nullable=True)
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    scraper_configs = relationship("ScraperConfig", back_populates="user", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")


class ScraperConfig(Base):
    __tablename__ = "scraper_configs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    county = Column(String(128), nullable=False)
    state = Column(String(2), nullable=False)
    record_type = Column(String(64), nullable=False)
    fields = Column(JSON, nullable=False, default=list)
    enrichment = Column(JSON, nullable=False, default=list)
    schedule = Column(JSON, nullable=False, default=dict)
    deliver = Column(JSON, nullable=False, default=dict)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    user = relationship("User", back_populates="scraper_configs")
    jobs = relationship("Job", back_populates="scraper_config", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scraper_config_id = Column(UUID(as_uuid=False), ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    trigger = Column(String(32), nullable=False, default="manual")  # manual | scheduled | test
    page_current = Column(Integer, nullable=False, default=0)
    page_total = Column(Integer, nullable=False, default=0)
    record_count = Column(Integer, nullable=False, default=0)
    export_key = Column(String(512), nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="jobs")
    scraper_config = relationship("ScraperConfig", back_populates="jobs")
    results = relationship("Result", back_populates="job", cascade="all, delete-orphan")
    logs = relationship("JobLog", back_populates="job", cascade="all, delete-orphan")


class Result(Base):
    __tablename__ = "results"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    date_recorded = Column(String(32), nullable=True)
    party_name = Column(String(512), nullable=True)
    heirs = Column(Text, nullable=True)
    legal_description = Column(Text, nullable=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    mailing_address = Column(String(512), nullable=True)
    enrichment_data = Column(JSON, nullable=True, default=dict)
    raw_html_hash = Column(String(32), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    job = relationship("Job", back_populates="results")


class CountyConnector(Base):
    __tablename__ = "county_connectors"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    county = Column(String(128), nullable=False, index=True)
    state = Column(String(2), nullable=False, index=True)
    record_types = Column(JSON, nullable=False, default=list)
    scraper_class = Column(String(255), nullable=False)
    scraper_mode = Column(String(16), nullable=False, default="ai")  # ai | manual
    render_mode = Column(String(16), nullable=False, default="playwright")  # playwright | static
    base_url = Column(String(512), nullable=False)
    gis_endpoint = Column(Text, nullable=True)  # Free ArcGIS REST API URL
    assessor_url = Column(Text, nullable=True)  # County assessor website (AI fallback)
    health_status = Column(String(16), nullable=False, default="unknown")  # healthy | degraded | down | unknown
    last_checked = Column(DateTime(timezone=True), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class JobLog(Base):
    __tablename__ = "job_logs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level = Column(String(16), nullable=False, default="info")  # info | success | warning | error
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    job = relationship("Job", back_populates="logs")


class CountyRecord(Base):
    __tablename__ = "county_records"

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    county = Column(String(64), nullable=False, index=True)
    state = Column(String(4), nullable=False, index=True)
    doc_type = Column(String(128), nullable=True)
    date_recorded = Column(String(32), nullable=True)
    party_name = Column(String(512), nullable=True)
    heirs = Column(Text, nullable=True)
    legal_description = Column(Text, nullable=True)
    parcel_id = Column(String(64), nullable=True)
    property_address = Column(String(512), nullable=True)
    mailing_address = Column(String(512), nullable=True)
    enrichment_data = Column(JSON, nullable=True, default=dict)
    record_hash = Column(String(32), nullable=False, unique=True, index=True)
    scraped_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    batch_date = Column(Date, server_default=func.current_date(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UserRecordView(Base):
    __tablename__ = "user_record_views"
    __table_args__ = (
        UniqueConstraint("user_id", "scraper_config_id", name="uq_user_scraper_view"),
    )

    id = Column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scraper_config_id = Column(UUID(as_uuid=False), ForeignKey("scraper_configs.id", ondelete="CASCADE"), nullable=False, index=True)
    last_viewed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
