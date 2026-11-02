"""Unit test: _fail_job must return the _set_status CAS boolean.

This test patches _set_status and _publish_log so it runs without any DB or
Redis connection (pure local execution). The done-path emit is verified by the
integration test in Task 7's endpoint round-trip and by manual proof.
"""
from unittest.mock import patch

from src.workers.tasks_helpers.status import _fail_job


class _DB:
    """Records the transaction calls _fail_job makes (it commits the CAS itself)."""

    def __init__(self):
        self.calls = []

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")

    def refresh(self, _obj):
        self.calls.append("refresh")


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
        db = _DB()
        result = _fail_job(db, _Job(), _R(), "j1", "boom")
    assert result is True
    assert calls["status"] == "failed"
    # the landed CAS is committed (release_quota_reservation's own calls follow)
    assert db.calls[:3] == ["rollback", "commit", "refresh"]


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
        db = _DB()
        result = _fail_job(db, _Job(), _R(), "j2", "too late")
    assert result is False
    assert "commit" not in db.calls  # a CAS that did not land is rolled back, never committed


def test_fail_job_rolls_back_when_the_commit_fails():
    """A failed commit is not a landed CAS, and the session is left usable (Codex 5d-i P2)."""
    class _Job:
        id = "j4"
        status = "scraping"

    class _FailingCommitDB(_DB):
        def commit(self):
            self.calls.append("commit")
            raise RuntimeError("server closed the connection")

    class _R:
        def publish(self, *a, **k): pass

    db = _FailingCommitDB()
    with patch("src.workers.tasks_helpers.status._set_status", lambda *a, **k: True),          patch("src.workers.tasks_helpers.status._publish_log"):
        result = _fail_job(db, _Job(), _R(), "j4", "boom")
    assert result is False
    assert db.calls[:3] == ["rollback", "commit", "rollback"]


def test_fail_job_keeps_a_committed_cas_when_the_refresh_fails():
    """A refresh error after the commit must not read as "did not land" (Codex 5d P1)."""
    class _Job:
        id = "j3"
        status = "scraping"

    class _FlakyRefreshDB(_DB):
        def refresh(self, _obj):
            raise RuntimeError("connection dropped after commit")

    class _R:
        def publish(self, *a, **k): pass

    with patch("src.workers.tasks_helpers.status._set_status", lambda *a, **k: True),          patch("src.workers.tasks_helpers.status._publish_log"):
        result = _fail_job(_FlakyRefreshDB(), _Job(), _R(), "j3", "boom")
    assert result is True
