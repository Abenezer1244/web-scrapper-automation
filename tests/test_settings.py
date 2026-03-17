"""Tests for src/config/settings.py — Pydantic BaseSettings."""
import pytest
from pydantic import ValidationError

from src.config import settings, Settings


# ─── Singleton sanity ─────────────────────────────────────────────────────────

def test_settings_loaded():
    """The singleton must have loaded without raising."""
    assert settings is not None


def test_base_dir_exists():
    assert settings.BASE_DIR.exists()


def test_exports_dir_is_under_base():
    assert settings.BASE_DIR in settings.EXPORTS_DIR.parents


def test_logs_dir_is_under_base():
    assert settings.BASE_DIR in settings.LOGS_DIR.parents


# ─── Scraping defaults ────────────────────────────────────────────────────────

def test_default_timeout_positive():
    assert settings.DEFAULT_TIMEOUT > 0


def test_max_retries_positive():
    assert settings.MAX_RETRIES > 0


def test_polite_delay_non_negative():
    assert settings.POLITE_DELAY_MS >= 0


# ─── Export ───────────────────────────────────────────────────────────────────

def test_export_format_valid():
    assert settings.EXPORT_FORMAT in ("csv", "json", "excel", "xlsx")


# ─── Plan limits ─────────────────────────────────────────────────────────────

def test_plan_limits_has_all_tiers():
    for plan in ("starter", "pro", "business", "agency"):
        assert plan in Settings.PLAN_LIMITS


def test_starter_limit():
    assert Settings.PLAN_LIMITS["starter"] == 50


def test_pro_limit():
    assert Settings.PLAN_LIMITS["pro"] == 500


def test_business_limit():
    assert Settings.PLAN_LIMITS["business"] == 5000


def test_agency_unlimited():
    assert Settings.PLAN_LIMITS["agency"] == -1


# ─── SECRET_KEY validator ─────────────────────────────────────────────────────

def test_secret_key_length_sufficient():
    """The loaded SECRET_KEY must be at least 32 chars (validator already ran)."""
    assert len(settings.SECRET_KEY) >= 32


def test_secret_key_validator_rejects_short_key(monkeypatch):
    """Validator must raise if key is shorter than 32 characters."""
    monkeypatch.setenv("SECRET_KEY", "tooshort")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://x:x@localhost/x")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    with pytest.raises(ValidationError, match="SECRET_KEY must be at least 32"):
        Settings(_env_file=None)


def test_secret_key_validator_rejects_default(monkeypatch):
    """Validator must reject known insecure default values."""
    monkeypatch.setenv("SECRET_KEY", "changeme")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
    monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql+psycopg2://x:x@localhost/x")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    with pytest.raises(ValidationError, match="SECRET_KEY must be at least 32"):
        Settings(_env_file=None)


# ─── CORS helper ─────────────────────────────────────────────────────────────

def test_get_allowed_origins_returns_list():
    origins = settings.get_allowed_origins()
    assert isinstance(origins, list)
    assert len(origins) >= 1


def test_get_allowed_origins_strips_whitespace(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", " https://app.bridgeleads.io , https://test.bridgeleads.io ")
    origins = settings.get_allowed_origins()
    assert "https://app.bridgeleads.io" in origins
    assert "https://test.bridgeleads.io" in origins
    for o in origins:
        assert o == o.strip()


# ─── ensure_dirs ─────────────────────────────────────────────────────────────

def test_ensure_dirs_creates_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setattr(type(settings), "LOGS_DIR", tmp_path / "logs")
    settings.ensure_dirs()
    assert (tmp_path / "exports").exists()
    assert (tmp_path / "logs").exists()
