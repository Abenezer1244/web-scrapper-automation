"""Shared fixtures for BridgeLeads test suite.

All fixtures use real infrastructure (Postgres, Redis) — no mocks.
The CI environment sets DATABASE_URL and DATABASE_URL_SYNC to a dedicated
test database so production data is never touched.
"""
import asyncio
import uuid

import pytest
import pytest_asyncio
import redis as sync_redis
from httpx import ASGITransport, AsyncClient
from main import app
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import src.db.session as _db_session
from src.api.auth import create_secure_token, hash_password
from src.config import settings
from src.db.models import Job, JobLog, PropertyListMembership, Result, ScraperConfig, User

# ─── Session-scoped event loop ────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop(request):  # noqa: ARG001
    """Single event loop shared by all tests and async fixtures in the session.

    Without this, pytest-asyncio creates a new event loop per test function
    while async fixtures (db, starter_user, etc.) run in the session-scoped
    loop set by asyncio_default_fixture_loop_scope = "session". This mismatch
    causes 'Future attached to a different loop' errors whenever a test body
    calls await db.commit(), and 'Event loop is closed' errors for the Redis
    client singleton that was created in the previous test's loop.
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ─── Test engine override ─────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_test_engine():
    """Replace the module-level pooled engine with NullPool for test isolation.

    The production engine uses a connection pool whose connections are bound to
    the event loop that first used them. With function-scoped async fixtures each
    test runs in a new loop, causing 'Future attached to a different loop' errors.

    NullPool creates a fresh, independent connection for every AsyncSessionLocal()
    call and closes it immediately afterwards — zero pool state carried between
    tests.
    """
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    _db_session.async_engine = engine
    _db_session.AsyncSessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await engine.dispose()


# ─── Database fixture ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db() -> AsyncSession:
    """Yield a real async DB session, then clean up all test rows."""
    # Use the module reference so we always pick up the NullPool sessionmaker
    # installed by _setup_test_engine above, not the original pooled one.
    async with _db_session.AsyncSessionLocal() as session:
        yield session
        # Clean up in FK-safe order; cascade handles children
        await session.execute(delete(JobLog))
        await session.execute(delete(Result))
        await session.execute(delete(PropertyListMembership))
        await session.execute(delete(Job))
        await session.execute(delete(ScraperConfig))
        await session.execute(delete(User).where(User.email.like("%@test.bridgeleads.io")))
        await session.commit()


# ─── HTTP client fixture ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """ASGI test client wired directly to the FastAPI app."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ─── Redis fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
def redis_client() -> sync_redis.Redis:
    """Real Redis connection for inspecting/clearing state in tests."""
    return sync_redis.from_url(settings.REDIS_URL, decode_responses=True)


# ─── User factories ───────────────────────────────────────────────────────────

def _test_email() -> str:
    return f"test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"


@pytest_asyncio.fixture
async def starter_user(db: AsyncSession) -> User:
    """A real starter-plan user in the DB."""
    user = User(
        id=str(uuid.uuid4()),
        email=_test_email(),
        password_hash=hash_password("TestPass123!"),
        plan="starter",
        records_used=0,
        records_limit=50,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture
async def business_user(db: AsyncSession) -> User:
    """A real business-plan user in the DB."""
    user = User(
        id=str(uuid.uuid4()),
        email=_test_email(),
        password_hash=hash_password("TestPass123!"),
        plan="business",
        records_used=0,
        records_limit=5000,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
def starter_token(starter_user: User) -> str:
    """JWT bearer token for the starter user."""
    return create_secure_token(starter_user.id)


@pytest.fixture
def business_token(business_user: User) -> str:
    """JWT bearer token for the business user."""
    return create_secure_token(business_user.id)


# ─── Scraper config factory ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def scraper_config(db: AsyncSession, starter_user: User) -> ScraperConfig:
    """A real scraper config row belonging to starter_user."""
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        name="Test Pierce Probate",
        county="pierce",
        state="WA",
        record_type="probate",
        fields=["party_name", "parcel_id"],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


# ─── Job factory ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def pending_job(db: AsyncSession, starter_user: User, scraper_config: ScraperConfig) -> Job:
    """A real pending job row."""
    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="pending",
        trigger="manual",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job
