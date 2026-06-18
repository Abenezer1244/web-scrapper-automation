"""Unit test: _fail_job must return the _set_status CAS boolean.

This test patches _set_status and _publish_log so it runs without any DB or
Redis connection (pure local execution). The done-path emit is verified by the
integration test in Task 7's endpoint round-trip and by manual proof.
"""
from unittest.mock import patch
from src.workers.tasks_helpers.status import _fail_job


def test_fail_job_returns_cas_result():
    """_fail_job must return the _set_status CAS boolean so callers can gate emit."""
    class _Job:
        id = "j1"
        status = "scraping"
    calls = {}

    def _fake_set_status(db, job, status, **kw):
        calls["status"] = status
        return True  # CAS succeeded

    class _R:
        def publish(self, *a, **k): pass

    with patch("src.workers.tasks_helpers.status._set_status", _fake_set_status), \
         patch("src.workers.tasks_helpers.status._publish_log"):
        result = _fail_job(object(), _Job(), _R(), "j1", "boom")
    assert result is True
    assert calls["status"] == "failed"


def test_fail_job_returns_false_when_cas_fails():
    """_fail_job returns False when _set_status CAS fails (job already terminal)."""
    class _Job:
        id = "j2"
        status = "done"

    def _fake_set_status_false(db, job, status, **kw):
        return False  # CAS rejected — job already terminal

    class _R:
        def publish(self, *a, **k): pass

    with patch("src.workers.tasks_helpers.status._set_status", _fake_set_status_false), \
         patch("src.workers.tasks_helpers.status._publish_log"):
        result = _fail_job(object(), _Job(), _R(), "j2", "too late")
    assert result is False
