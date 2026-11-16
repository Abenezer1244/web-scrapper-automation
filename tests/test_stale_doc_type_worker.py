"""A saved selection the county no longer offers fails the job in the worker (#644).

The real run_scrape_job against the real test DB and Redis, as in
tests/test_job_degradations_wiring.py (whose make_job fixture it reuses). The county
portal is replaced, as there: here it does what a scraper constructor does with a stale
selection, calling the real strict check on Chelan's real registry entry, which raises
StaleDocTypeSelectionError (#625 retired Notice of Default for Chelan). The job must
fail with the customer copy, never be retried, and still be reported to error tracking.
"""
from __future__ import annotations

# make_job and tracking are fixtures imported from other test modules and taken as
# parameter names, which ruff reads as redefining the import. Only F811, only here.
# ruff: noqa: F811
from sqlalchemy import text

from src.db.session import SyncSessionLocal
from src.scrapers.doc_types import StaleDocTypeSelectionError, canonical_tokens_or_raise
from src.workers import tasks
from tests.test_error_tracking_jobs import _events, tracking  # noqa: F401 -- fixture
from tests.test_job_degradations_wiring import _apply, make_job  # noqa: F401 -- fixture

_STALE = ["notice_of_default", "notice_of_trustee_sale"]


def test_a_stale_selection_fails_the_job_with_customer_copy_and_no_retry(
        make_job, redis_client, monkeypatch, tracking):
    async def _constructor_check(*_a, **_k):
        canonical_tokens_or_raise("chelan", "wa", _STALE)  # raises: notice_of_default retired
        raise AssertionError("a stale selection must not reach the portal")

    monkeypatch.setattr(tasks, "_run_scraper", _constructor_check)
    job_id = make_job("pre_foreclosure")
    _apply(job_id)

    with SyncSessionLocal() as s:
        job = s.execute(text(
            "SELECT status, error_message, retry_count, next_retry_at, billed_count, reserved_count "
            "FROM jobs WHERE id = :j"), {"j": job_id}).one()
        logs = [m for (m,) in s.execute(text(
            "SELECT message FROM job_logs WHERE job_id = :j ORDER BY created_at"), {"j": job_id}).all()]

    expected = StaleDocTypeSelectionError("chelan", "wa", ["notice_of_default"]).public_message
    assert job.status == "failed"
    assert job.error_message == expected
    assert (job.retry_count or 0) == 0 and job.next_retry_at is None  # never re-queued
    assert not job.billed_count and not job.reserved_count             # nothing billed or held
    assert not any("retry" in m.lower() for m in logs)
    # Reported once as a failed job (a registry change shipped without repairing saved
    # selections); the SourceUnavailableError branch beside it reports nothing.
    import sentry_sdk

    sentry_sdk.flush()
    events = _events({"envelopes": tracking})
    assert [e["tags"].get("job_outcome") for e in events if "job_outcome" in e.get("tags", {})] == ["failed"]
