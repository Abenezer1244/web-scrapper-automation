"""S3-15 (audit #3, re-confirmed audit #5): the Tracerfy webhook body cannot raise a
customer's bill, and its download URL is fetched only from Tracerfy's own bucket,
over HTTPS.

The body is authenticated by a shared secret that audit #3 found in edge logs
(S3-16), so it is treated as attacker-controllable here. DB-backed against real
rows through the same harness as tests/test_tracerfy_ingest.py; only the CSV
download is stubbed (no network, no credits).
"""
import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.workers.tracerfy_ingest import _host_is_tracerfy, ingest_tracerfy_batch
from tests.test_tracerfy_ingest import (
    DOWNLOAD_URL,
    _csv,
    _next_queue_id,
    _seed,
    _usage,
)


@pytest.fixture
def stub_csv(monkeypatch):
    """Serve a canned result CSV instead of fetching one. No network."""
    def _install(csv_text: str):
        monkeypatch.setattr(
            "src.scrapers.enrichment.skip_trace.download_tracerfy_csv", lambda url: csv_text
        )
    return _install


def _stored_rows_uploaded(queue_id: int) -> int:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT rows_uploaded FROM skip_trace_queues WHERE tracerfy_queue_id = :q"),
            {"q": queue_id},
        ).scalar_one()


def _set_stored_rows_uploaded(queue_id: int, n: int) -> None:
    with system_sync_session() as db:
        db.execute(
            text("UPDATE skip_trace_queues SET rows_uploaded = :n WHERE tracerfy_queue_id = :q"),
            {"n": n, "q": queue_id},
        )
        db.commit()


_TWO_ADDRESSES = [("1 FIRST ST", "TACOMA", "WA"), ("2 SECOND ST", "TACOMA", "WA")]
# Tracerfy answers the first address only; the second is 'unmatched'.
_ONE_HIT = "1 FIRST ST,TACOMA,WA,JANE,DOE,2065550100,Mobile,2065550100,,,,"


@pytest.mark.asyncio
async def test_a_webhook_cannot_raise_rows_uploaded_to_bill_a_dropped_row(starter_user, stub_csv):
    """Tracerfy accepted 1 of the 2 rows we sent (it drops and de-duplicates), so the
    unmatched row was never looked up and must not be billed. A forged body saying
    999 used to overwrite that and bill it."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    _set_stored_rows_uploaded(qid, 1)
    stub_csv(_csv(_ONE_HIT))
    before = _usage(starter_user.id)

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=999, credits_deducted=999
    )

    assert _stored_rows_uploaded(qid) == 1
    assert _usage(starter_user.id) - before == 1  # the hit only


@pytest.mark.asyncio
async def test_a_webhook_can_still_report_a_dropped_row(starter_user, stub_csv):
    """Lowering is the real provider signal (and only ever bills less): honoured."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)  # stored rows_uploaded = 2
    stub_csv(_csv(_ONE_HIT))
    before = _usage(starter_user.id)

    ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=1, credits_deducted=1)

    assert _stored_rows_uploaded(qid) == 1
    assert _usage(starter_user.id) - before == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-5, "999", 3.5, None, True])
async def test_a_malformed_count_bills_completed_rows_only(starter_user, stub_csv, bad):
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    stub_csv(_csv(_ONE_HIT))
    before = _usage(starter_user.id)

    ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=bad, credits_deducted=bad)

    assert _stored_rows_uploaded(qid) == 0
    assert _usage(starter_user.id) - before == 1


@pytest.mark.parametrize("url", [
    "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/x.csv",
    "https://tracerfy.nyc3.digitaloceanspaces.com/tracerfy/x.csv",
])
def test_tracerfys_own_bucket_is_trusted(url):
    assert _host_is_tracerfy(url) is True


@pytest.mark.parametrize("url", [
    # plaintext: the CSV is customers' contacts
    "http://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/x.csv",
    # a bucket called "tracerfy" in another region can be anyone's
    "https://tracerfy.sfo3.digitaloceanspaces.com/x.csv",
    "https://tracerfy.fra1.cdn.digitaloceanspaces.com/x.csv",
    "https://tracerfy.evil.digitaloceanspaces.com/x.csv",
    "https://evil.nyc3.cdn.digitaloceanspaces.com/x.csv",
])
def test_other_buckets_and_plaintext_are_refused(url):
    assert _host_is_tracerfy(url) is False
