"""Unit tests for the R2 export-upload retry helper (Fix 1: no silent strand).

A failed export upload must not leave the job done+billed with no deliverable.
The helper retries transient R2 failures and reports success/failure honestly so
run_scrape_job can fail the job loudly instead. Real R2 is an external paid API,
so the exporter is a hand-rolled stub (no network) — the rule's external-API
exception to the no-mocks policy.
"""
import src.workers.tasks as tasks


class _FlakyExporter:
    """Stub exporter whose upload fails the first `fail_times` calls."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def upload_to_r2(self, local_file, object_key):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"R2 unavailable (call {self.calls})")
        return object_key


def test_upload_succeeds_first_try(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda *_: None)
    exp = _FlakyExporter(fail_times=0)
    ok, exc = tasks._upload_export_with_retry(exp, "leads.csv", "exports/u/j/leads.csv")
    assert ok is True
    assert exc is None
    assert exp.calls == 1  # no needless retries


def test_upload_recovers_after_transient_failures(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda *_: None)
    exp = _FlakyExporter(fail_times=tasks._R2_UPLOAD_ATTEMPTS - 1)
    ok, exc = tasks._upload_export_with_retry(exp, "leads.csv", "exports/u/j/leads.csv")
    assert ok is True
    assert exc is None
    assert exp.calls == tasks._R2_UPLOAD_ATTEMPTS  # last attempt succeeded


def test_upload_gives_up_after_all_attempts(monkeypatch):
    monkeypatch.setattr(tasks.time, "sleep", lambda *_: None)
    exp = _FlakyExporter(fail_times=999)
    ok, exc = tasks._upload_export_with_retry(exp, "leads.csv", "exports/u/j/leads.csv")
    assert ok is False
    assert isinstance(exc, RuntimeError)
    assert exp.calls == tasks._R2_UPLOAD_ATTEMPTS  # bounded, no infinite loop
