"""Database latency canary (src/workers/db_canary.py).

Regression cover for 2026-10-01: the database was slow for ~6 hours before login
broke, and nothing was watching. No mocks (per .claude/rules/testing.md): the
healthy case probes the real test database, the outage case a real engine on a
port where nothing listens, and the streak lives in the real test Redis. The
alert is observed through the durable audit row send_ops_alert always writes.
"""

from datetime import UTC, datetime

import pytest
import redis as sync_redis
from sqlalchemy import create_engine, delete, select
from sqlalchemy.pool import NullPool

import src.db.session as db_session
from src.config import settings
from src.db.models import AuditEvent
from src.workers import db_canary
from src.workers.ops_alerts import _COOLDOWN_PREFIX

_ALERT_PATH = "db_latency:primary"
_RECOVERED_PATH = "db_latency:recovered"
_COOLDOWN_KEY = f"{_COOLDOWN_PREFIX}db_latency:primary"
# Port 1 on loopback: nothing listens, so the connect is refused at once.
_REFUSED_URL = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/nonexistent_test"


def _redis(url: str | None = None):
    return sync_redis.from_url(url or settings.REDIS_URL, **settings.redis_kwargs())


def _alert_rows(since: datetime, path: str = _ALERT_PATH) -> list[AuditEvent]:
    with db_session.system_sync_session() as db:
        return list(db.scalars(
            select(AuditEvent).where(
                AuditEvent.event == "ops_alert",
                AuditEvent.path == path,
                AuditEvent.created_at >= since,
            )
        ))


@pytest.fixture(autouse=True)
def _clean_state():
    """No streak, cooldown or alert row leaks between tests. The URL is captured
    now: a test may point settings.REDIS_URL at a dead port, and this teardown
    can run before that patch is undone."""
    redis_url = settings.REDIS_URL
    _redis(redis_url).delete(db_canary._STREAK_KEY, _COOLDOWN_KEY)
    yield
    _redis(redis_url).delete(db_canary._STREAK_KEY, _COOLDOWN_KEY)
    with db_session.system_sync_session() as db:
        db.execute(delete(AuditEvent).where(AuditEvent.path.in_([_ALERT_PATH, _RECOVERED_PATH])))
        db.commit()


@pytest.fixture
def database_down(monkeypatch):
    engine = create_engine(_REFUSED_URL, poolclass=NullPool, connect_args={"connect_timeout": 2})
    monkeypatch.setattr(db_session, "canary_engine", engine)
    yield
    engine.dispose()


def test_healthy_database_answers_and_clears_the_streak():
    _redis().set(db_canary._STREAK_KEY, 2)

    stats = db_canary.run_db_latency_canary()

    assert stats["answered"] is True
    assert stats["error"] is None
    assert stats["seconds"] < db_canary._SLOW_SECONDS
    assert stats["streak"] == 0
    assert _redis().get(db_canary._STREAK_KEY) is None


def test_alerts_on_the_third_bad_probe_and_not_before(database_down):
    since = datetime.now(UTC)

    first = db_canary.run_db_latency_canary()
    second = db_canary.run_db_latency_canary()
    assert (first["streak"], second["streak"]) == (1, 2)
    assert first["answered"] is False and first["error"] == "OperationalError"
    assert _alert_rows(since) == []

    third = db_canary.run_db_latency_canary()

    assert third["streak"] == 3
    rows = _alert_rows(since)
    assert len(rows) == 1
    assert "Database is not answering (OperationalError)" in rows[0].detail


def test_one_good_probe_resets_the_streak(database_down, monkeypatch):
    since = datetime.now(UTC)
    db_canary.run_db_latency_canary()
    db_canary.run_db_latency_canary()

    monkeypatch.undo()  # the real test database again
    assert db_canary.run_db_latency_canary()["streak"] == 0

    engine = create_engine(_REFUSED_URL, poolclass=NullPool, connect_args={"connect_timeout": 2})
    monkeypatch.setattr(db_session, "canary_engine", engine)
    assert db_canary.run_db_latency_canary()["streak"] == 1
    assert _alert_rows(since) == []
    engine.dispose()


def test_no_alert_when_the_streak_cannot_be_counted(database_down, monkeypatch):
    """Redis down: a blip cannot be told from an outage, and the alert cooldown
    fails open, so alerting would mean an e-mail every 2 minutes."""
    since = datetime.now(UTC)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")

    for _ in range(db_canary._ALERT_AFTER + 1):
        stats = db_canary.run_db_latency_canary()
        assert stats["streak"] is None
        assert stats["alerted"] is False

    assert _alert_rows(since) == []


def test_a_slow_answer_counts_as_bad(monkeypatch):
    """A database that answers, but slower than the threshold, is degraded: that
    is the six hours before the 2026-10-01 cliff. The threshold is lowered so the
    real test database is 'slow'."""
    monkeypatch.setattr(db_canary, "_SLOW_SECONDS", 0.0)
    since = datetime.now(UTC)

    for _ in range(db_canary._ALERT_AFTER):
        stats = db_canary.run_db_latency_canary()
        assert stats["answered"] is True and stats["error"] is None

    assert stats["streak"] == db_canary._ALERT_AFTER
    rows = _alert_rows(since)
    assert len(rows) == 1
    assert "Database is slow" in rows[0].detail


def test_recovery_after_an_alert_says_so_and_rearms_the_alert(database_down, monkeypatch):
    """Without clearing the outage alert's cooldown, an outage that came back
    inside the cooldown window would reach the threshold and send nothing."""
    since = datetime.now(UTC)
    for _ in range(db_canary._ALERT_AFTER):
        db_canary.run_db_latency_canary()
    _redis().set(_COOLDOWN_KEY, "1")  # what a delivered alert leaves behind

    monkeypatch.undo()  # the real test database again
    stats = db_canary.run_db_latency_canary()

    assert stats["streak"] == 0
    assert len(_alert_rows(since, _RECOVERED_PATH)) == 1
    assert _redis().get(_COOLDOWN_KEY) is None


def test_no_recovery_notice_after_a_blip():
    """A good probe after fewer bad ones than the alert threshold was never
    announced, so there is nothing to call recovered."""
    since = datetime.now(UTC)
    _redis().set(db_canary._STREAK_KEY, db_canary._ALERT_AFTER - 1)

    db_canary.run_db_latency_canary()

    assert _alert_rows(since, _RECOVERED_PATH) == []


@pytest.mark.parametrize(
    "asyncpg_url, expected_query",
    [
        ("postgresql+asyncpg://u:p@db.example.com:5432/app", {}),
        ("postgresql+asyncpg://u:p@db.example.com:5432/app?ssl=verify-full",
         {"sslmode": "verify-full"}),
        ("postgresql+asyncpg://u:p@db.example.com:5432/app?sslmode=require&target_session_attrs=any",
         {"sslmode": "require", "target_session_attrs": "any"}),
        # Both given: sslmode wins and ssl is removed (libpq rejects it outright).
        ("postgresql+asyncpg://u:p@db.example.com:5432/app?ssl=require&sslmode=verify-full",
         {"sslmode": "verify-full"}),
    ],
)
def test_canary_url_keeps_the_apis_parameters(asyncpg_url, expected_query):
    """asyncpg's ssl= is libpq's sslmode=; nothing else is dropped, so a stricter
    TLS mode on the API is the mode the probe uses too."""
    url = db_session._libpq_url(asyncpg_url)
    assert url.drivername == "postgresql+psycopg2"
    assert dict(url.query) == expected_query
    assert (url.host, url.port, url.database) == ("db.example.com", 5432, "app")


@pytest.mark.parametrize(
    "asyncpg_url, expected",
    [
        ("postgresql+asyncpg://u:p@h:5432/app", "-c statement_timeout=5000"),
        ("postgresql+asyncpg://u:p@h:5432/app?options=-c%20search_path%3Dapp",
         "-c search_path=app -c statement_timeout=5000"),
    ],
)
def test_canary_keeps_the_urls_session_options(asyncpg_url, expected):
    assert db_session._canary_options(db_session._libpq_url(asyncpg_url)) == expected


def test_canary_engine_dials_the_api_path_without_a_pool():
    """Fresh connection per probe (the pooler login is what failed), on the
    API's DATABASE_URL host and port, never the worker's :6543 pool."""
    assert isinstance(db_session.canary_engine.pool, NullPool)
    api_url = db_session.async_engine.url
    canary_url = db_session.canary_engine.url
    assert canary_url.drivername == "postgresql+psycopg2"
    assert (canary_url.host, canary_url.port, canary_url.database) == (
        api_url.host, api_url.port, api_url.database,
    )


def test_beat_runs_the_canary_every_two_minutes_and_drops_stale_probes():
    import src.workers.scheduler  # noqa: F401 — the module that defines beat_schedule
    from src.workers import app

    entry = app.conf.beat_schedule["db-latency-canary"]
    assert entry["task"] == "src.workers.db_canary.db_latency_canary"
    assert entry["schedule"] == 120.0
    assert entry["options"]["expires"] < entry["schedule"]
    assert "src.workers.db_canary" in app.conf.include
