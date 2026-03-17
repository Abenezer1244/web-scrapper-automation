import pytest
from pathlib import Path
from src.config import Settings


def test_base_dir_exists():
    assert Settings.BASE_DIR.exists()


def test_default_timeout_is_positive():
    assert Settings.DEFAULT_TIMEOUT > 0


def test_max_retries_is_positive():
    assert Settings.MAX_RETRIES > 0


def test_export_format_valid():
    assert Settings.EXPORT_FORMAT in ('csv', 'json', 'excel', 'xlsx')


def test_ensure_dirs_creates_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, 'EXPORTS_DIR', tmp_path / 'exports')
    monkeypatch.setattr(Settings, 'LOGS_DIR', tmp_path / 'logs')
    Settings.ensure_dirs()
    assert (tmp_path / 'exports').exists()
    assert (tmp_path / 'logs').exists()
