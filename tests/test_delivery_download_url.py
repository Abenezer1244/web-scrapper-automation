"""Tests for the delivery download-URL builder (`_delivery_download_url`).

BACKLOG §4 hardening: in production, when API_BASE_URL is unset the only
remaining path is the raw R2/S3 presign, which 401s in prod. The builder must
FAIL LOUDLY rather than fall back to a dead link. These tests pin that
contract:

  * API_BASE_URL set        -> mints a revocable app download-token link
  * prod + API_BASE_URL ""  -> raises RuntimeError (never calls the presign)
  * non-prod + ""           -> falls back to the presign (dev/test still works)

No mocks: we drive the real builder with real settings (toggled via the
`settings` singleton) and a tiny in-process exporter stand-in that records
whether the presign fallback was reached.
"""

import uuid

import pytest

from src.config import settings
from src.workers.tasks_helpers.status import _DELIVERY_TOKEN_TTL, _delivery_download_url


class _RecordingExporter:
    """Records whether get_download_url() (the presign fallback) was called."""

    def __init__(self) -> None:
        self.called_with = None

    def get_download_url(self, object_key: str, expires_in: int) -> str:
        self.called_with = (object_key, expires_in)
        return f"https://r2.example/presigned/{object_key}?exp={expires_in}"


@pytest.fixture
def restore_settings():
    """Snapshot + restore the two settings this builder reads."""
    saved = (settings.API_BASE_URL, settings.ENVIRONMENT)
    yield
    settings.API_BASE_URL, settings.ENVIRONMENT = saved


def test_mints_app_token_link_when_api_base_url_set(restore_settings):
    settings.API_BASE_URL = "https://api.bridgeleads.io/"  # trailing slash on purpose
    settings.ENVIRONMENT = "production"
    job_id = uuid.uuid4().hex
    exporter = _RecordingExporter()

    url = _delivery_download_url(job_id, uuid.uuid4(), "exports/x.csv", exporter)

    # rstrip('/') applied, app download route, token present, presign NOT used.
    assert url.startswith(f"https://api.bridgeleads.io/jobs/{job_id}/download?token=")
    assert exporter.called_with is None


def test_raises_in_production_when_api_base_url_blank(restore_settings):
    settings.API_BASE_URL = ""
    settings.ENVIRONMENT = "production"
    exporter = _RecordingExporter()

    with pytest.raises(RuntimeError, match="API_BASE_URL is required in production"):
        _delivery_download_url(uuid.uuid4().hex, uuid.uuid4(), "exports/x.csv", exporter)

    # The broken presign path must never be reached in prod.
    assert exporter.called_with is None


@pytest.mark.parametrize("env_value", ["Production", " production ", "PRODUCTION"])
def test_production_check_is_case_and_whitespace_insensitive(restore_settings, env_value):
    settings.API_BASE_URL = ""
    settings.ENVIRONMENT = env_value
    with pytest.raises(RuntimeError, match="API_BASE_URL is required in production"):
        _delivery_download_url(uuid.uuid4().hex, uuid.uuid4(), "exports/x.csv", _RecordingExporter())


def test_falls_back_to_presign_in_non_production(restore_settings):
    settings.API_BASE_URL = ""
    settings.ENVIRONMENT = "development"
    exporter = _RecordingExporter()

    url = _delivery_download_url(uuid.uuid4().hex, uuid.uuid4(), "exports/x.csv", exporter)

    assert exporter.called_with == ("exports/x.csv", _DELIVERY_TOKEN_TTL)
    assert url.startswith("https://r2.example/presigned/")
