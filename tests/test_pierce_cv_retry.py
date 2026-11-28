"""Tacoma code violations: a source still failing transiently after the per-page
retries must reach the worker as TransientScrapeError (job retried later), not a
plain RuntimeError (job failed for good). Permanent errors stay permanent.

Before 2026-11-27 every exhausted page fetch raised RuntimeError, so a short ArcGIS
outage failed the run with no job-level retry, and the backoff blocked the event loop.
"""
import pytest
import requests

from src.scrapers import pierce_wa_code_violation as pcv
from src.scrapers.reliability import TransientScrapeError, is_transient_scrape_error


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code, self._body = status, body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(pcv, "_RETRY_BACKOFF", (0, 0, 0))


def _serve(monkeypatch, make):
    calls = []

    def fake_get(*a, **kw):
        calls.append(1)
        return make()
    monkeypatch.setattr(pcv, "safe_get", fake_get)
    return calls


async def _scrape():
    return await pcv.PierceWACodeViolationScraper().scrape("11/01/2026", "11/02/2026")


async def test_persistent_503_is_transient_after_page_retries(monkeypatch):
    calls = _serve(monkeypatch, lambda: _Resp(503))
    with pytest.raises(TransientScrapeError) as ei:
        await _scrape()
    assert is_transient_scrape_error(ei.value)
    assert len(calls) == pcv.settings.MAX_RETRIES


async def test_persistent_timeout_is_transient(monkeypatch):
    def boom():
        raise requests.exceptions.ReadTimeout("read timed out")
    _serve(monkeypatch, boom)
    with pytest.raises(TransientScrapeError):
        await _scrape()


async def test_arcgis_error_body_is_transient(monkeypatch):
    _serve(monkeypatch, lambda: _Resp(200, {"error": {"message": "overloaded"}}))
    with pytest.raises(TransientScrapeError):
        await _scrape()


async def test_a_400_stays_permanent_and_is_not_retried(monkeypatch):
    calls = _serve(monkeypatch, lambda: _Resp(400))
    with pytest.raises(RuntimeError) as ei:
        await _scrape()
    assert not isinstance(ei.value, TransientScrapeError)
    assert len(calls) == 1


async def test_an_invalid_query_error_body_stays_permanent(monkeypatch):
    """ArcGIS answers an invalid query with HTTP 200 + error.code 400 (Codex High)."""
    calls = _serve(monkeypatch, lambda: _Resp(200, {"error": {"code": 400, "message": "bad"}}))
    with pytest.raises(RuntimeError) as ei:
        await _scrape()
    assert not isinstance(ei.value, TransientScrapeError)
    assert len(calls) == 1


async def test_a_throttle_error_body_is_still_transient(monkeypatch):
    _serve(monkeypatch, lambda: _Resp(200, {"error": {"code": 429, "message": "slow down"}}))
    with pytest.raises(TransientScrapeError):
        await _scrape()


async def test_a_recovered_page_still_returns_records(monkeypatch):
    pages = [_Resp(503), _Resp(200, {"features": []})]
    _serve(monkeypatch, lambda: pages.pop(0))
    assert await _scrape() == []
