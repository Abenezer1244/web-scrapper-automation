"""S3-15 (audit #3, re-confirmed audit #5): the Tracerfy webhook is a trigger, never a
source of data. Its secret has leaked (S3-16), and a URL on Tracerfy's bucket proves
nothing about WHICH queue it belongs to (any Tracerfy customer's result CSV would pass
a host check). So the CSV fetched and the counts billed come from Tracerfy's own
queue record, and the download host is pinned to Tracerfy's bucket over HTTPS.

DB-backed against real rows through the harness of tests/test_tracerfy_ingest.py; only
the provider calls (queue list, CSV download) are served locally (no network, no
credits).
"""
import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.enrichment.skip_trace import TracerfyError
from src.workers.tracerfy_ingest import _host_is_tracerfy, ingest_tracerfy_batch
from tests.test_tracerfy_ingest import DOWNLOAD_URL, _csv, _next_queue_id, _seed, _usage

_OTHER_CUSTOMERS_CSV = "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/someone-else.csv"
_TWO_ADDRESSES = [("1 FIRST ST", "TACOMA", "WA"), ("2 SECOND ST", "TACOMA", "WA")]
# Tracerfy answers the first address only; the second is 'unmatched'.
_ONE_HIT = "1 FIRST ST,TACOMA,WA,JANE,DOE,2065550100,Mobile,2065550100,,,,"


@pytest.fixture
def provider(monkeypatch):
    """Serve Tracerfy's queue list and result CSV locally; records every CSV URL
    fetched, so a test can prove WHICH file was read."""
    fetched: list[str] = []

    def _install(csv_text: str, *queues: dict):
        def _download(url):
            fetched.append(url)
            return csv_text
        monkeypatch.setattr("src.scrapers.enrichment.skip_trace.download_tracerfy_csv", _download)
        monkeypatch.setattr(
            "src.scrapers.enrichment.skip_trace.fetch_queues", lambda *a, **k: list(queues)
        )
        return fetched
    return _install


def _complete(qid: int, rows: int) -> dict:
    return {"id": qid, "pending": False, "download_url": DOWNLOAD_URL,
            "rows_uploaded": rows, "credits_deducted": rows}


def _queue(qid: int) -> tuple:
    with system_sync_session() as db:
        return tuple(db.execute(
            text("SELECT status, rows_uploaded FROM skip_trace_queues WHERE tracerfy_queue_id = :q"),
            {"q": qid},
        ).one())


@pytest.mark.asyncio
@pytest.mark.parametrize("body_rows", [999, 0, -5, "999", None])
async def test_the_webhook_body_never_decides_the_count(starter_user, provider, body_rows):
    """Tracerfy accepted 1 of the 2 rows sent (it drops and de-duplicates), so the
    unmatched row was never looked up. A body saying 999 used to bill it; a body
    saying 0 used to suppress billing. Only the provider's 1 counts."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    provider(_csv(_ONE_HIT), _complete(qid, 1))
    before = _usage(starter_user.id)

    ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=body_rows, credits_deducted=body_rows
    )

    assert _queue(qid) == ("completed", 1)
    assert _usage(starter_user.id) - before == 1  # the hit only


@pytest.mark.asyncio
async def test_the_webhook_body_never_chooses_the_csv(starter_user, provider):
    """A result URL on Tracerfy's own bucket passes the host pin whoever it belongs
    to. The file read is the one Tracerfy's record names for THIS queue."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    fetched = provider(_csv(_ONE_HIT), _complete(qid, 2))

    ingest_tracerfy_batch(queue_id=qid, download_url=_OTHER_CUSTOMERS_CSV, rows_uploaded=2)

    assert fetched == [DOWNLOAD_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("record", ["pending", "absent"])
async def test_a_queue_not_complete_at_the_provider_is_deferred_not_ingested(
    starter_user, provider, record
):
    """A premature or forged webhook: nothing fetched, nothing billed, and the queue
    stays 'pending' (never 'errored', which would block the genuine webhook)."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    queues = [{"id": qid, "pending": True, "download_url": None}] if record == "pending" else []
    fetched = provider(_csv(_ONE_HIT), *queues)
    before = _usage(starter_user.id)

    out = ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2)

    assert out["deferred"] == "provider_not_complete"
    assert fetched == []
    assert _queue(qid) == ("pending", 2)
    assert _usage(starter_user.id) == before


@pytest.mark.asyncio
async def test_after_the_last_recheck_it_pages_ops_and_still_does_not_error(starter_user, provider):
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    fetched = provider(_csv(_ONE_HIT))

    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, provider_rechecks=5
    )

    assert out["skipped"] == "provider_not_complete"
    assert fetched == []
    assert _queue(qid)[0] == "pending"


@pytest.mark.asyncio
async def test_an_unreachable_provider_defers_and_never_trusts_the_body(
    starter_user, provider, monkeypatch
):
    """An outage is neither a reason to fall back to the body nor to mark a genuine
    batch errored: nothing fetched, nothing billed, still 'pending'."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    fetched = provider(_csv(_ONE_HIT))

    def _down(*a, **k):
        raise TracerfyError("Tracerfy returned 503 for queue list")

    monkeypatch.setattr("src.scrapers.enrichment.skip_trace.fetch_queues", _down)
    out = ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2)

    assert out["deferred"] == "provider_not_complete"
    assert fetched == []
    assert _queue(qid)[0] == "pending"


@pytest.mark.asyncio
async def test_repeated_webhooks_share_one_recheck_chain(starter_user, provider, monkeypatch):
    """Each forged webhook used to start its own chain of provider calls. A trigger
    that finds a chain running makes no provider call at all."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    provider(_csv(_ONE_HIT))
    lookups: list[int] = []
    monkeypatch.setattr(
        "src.scrapers.enrichment.skip_trace.fetch_queues",
        lambda *a, **k: lookups.append(1) or [{"id": qid, "pending": True, "download_url": None}],
    )
    scheduled: list[dict] = []
    monkeypatch.setattr(ingest_tracerfy_batch, "apply_async", lambda **kw: scheduled.append(kw))

    first = ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL)
    later = [ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL) for _ in range(3)]

    assert first["deferred"] == "provider_not_complete"
    assert all(o["deferred"] == "provider_recheck_running" for o in later)
    assert len(lookups) == 1
    assert len(scheduled) == 1 and scheduled[0]["kwargs"]["provider_rechecks"] == 1

    # The chain's own re-check still runs, and once it gives up the queue is free
    # for the next genuine trigger.
    ingest_tracerfy_batch(queue_id=qid, download_url="", provider_rechecks=5)
    assert ingest_tracerfy_batch(queue_id=qid, download_url=DOWNLOAD_URL)["deferred"] == (
        "provider_not_complete"
    )
    assert len(lookups) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_url", [123, None, ["x"], "https://[::1/x.csv"])
async def test_a_malformed_body_cannot_fail_the_task(starter_user, provider, bad_url):
    """A malformed body value used to raise inside the host check, and autoretry's
    exhaustion marks the REAL queue errored, so the genuine webhook then no-ops."""
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    fetched = provider(_csv(_ONE_HIT), _complete(qid, 2))

    out = ingest_tracerfy_batch.run(queue_id=qid, download_url=bad_url, rows_uploaded=bad_url)

    assert out["hits"] == 1
    assert fetched == [DOWNLOAD_URL]


@pytest.mark.asyncio
async def test_a_failing_ops_alert_does_not_fail_the_task(starter_user, provider, monkeypatch):
    qid = _next_queue_id()
    _seed(starter_user.id, qid, _TWO_ADDRESSES)
    provider(_csv(_ONE_HIT))

    def _alert_down(*a, **k):
        raise RuntimeError("mail provider down")

    monkeypatch.setattr("src.workers.ops_alerts.send_ops_alert", _alert_down)
    out = ingest_tracerfy_batch(
        queue_id=qid, download_url=DOWNLOAD_URL, rows_uploaded=2, provider_rechecks=5
    )

    assert out["skipped"] == "provider_not_complete"
    assert _queue(qid)[0] == "pending"


@pytest.mark.parametrize("url", [
    "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/x.csv",
    "https://tracerfy.nyc3.digitaloceanspaces.com/tracerfy/x.csv",
    "https://TRACERFY.NYC3.CDN.DIGITALOCEANSPACES.COM/tracerfy/x.csv",
    "https://tracerfy.nyc3.cdn.digitaloceanspaces.com:443/tracerfy/x.csv",
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
    # a port or credentials in the authority
    "https://tracerfy.nyc3.cdn.digitaloceanspaces.com:8443/x.csv",
    "https://user@tracerfy.nyc3.cdn.digitaloceanspaces.com/x.csv",
    "https://tracerfy.nyc3.cdn.digitaloceanspaces.com./x.csv",
    "not a url",
    "https://[::1/x.csv",
])
def test_other_buckets_and_plaintext_are_refused(url):
    assert _host_is_tracerfy(url) is False
