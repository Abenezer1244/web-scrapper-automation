"""After a failure, inline enrichment still holds no transaction across its next lookup.

A rollback expires every row the session holds, even with expire_on_commit=False. The next
plain READ of one of them (a parcel, a name, `config.county`) silently opens a transaction,
and in five places that read happened right before a slow network call, so the transaction
sat idle across it and the pooler could drop it, losing the writes that followed (the
canary bug of #579; Codex audit of enrich.py, Phase 2). Each test fails one step for real
(the driver refuses a NUL byte, or a query fails at the cursor), then checks that the next
lookup runs with no transaction open.

Real DB and the real enrichment steps; only the network calls are replaced.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, select, text

from src.db.models import Result, ScraperConfig
from src.scrapers.enrichment import pacs
from src.workers.tasks_helpers import enrich
from tests.test_address_lookup_progress import _all_answered, _batch, _county, _enrich, _job
from tests.test_skip_trace_enqueue_after_delivery import _job as _plain_job
from tests.test_skip_trace_enqueue_after_delivery import _row

_WORKER_PIDS: set[int] = set()


@pytest.fixture(autouse=True)
def _record_worker_backends():
    """The server pid of every connection the worker code checks out during a test, so the
    probe below looks at the job's own connections and nothing else in the database."""
    from src.db.session import sync_engine

    def _checkout(dbapi_connection, *_a):
        _WORKER_PIDS.add(dbapi_connection.get_backend_pid())

    _WORKER_PIDS.clear()
    event.listen(sync_engine, "checkout", _checkout)
    yield
    event.remove(sync_engine, "checkout", _checkout)


def _idle_in_transaction() -> int:
    """The job's connections sitting idle inside a transaction right now, read from a fresh
    connection (and transaction) so the server's activity snapshot is current."""
    from src.db.session import sync_engine

    with sync_engine.connect() as probe:
        return probe.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE pid = ANY(:pids) "
            "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"),
            {"pids": sorted(_WORKER_PIDS)}).scalar()


class SoftTimeLimitExceeded(Exception):  # noqa: N818 -- Celery's own class name, matched by name
    """What Celery raises asynchronously into whatever line the task is on."""


@pytest.mark.parametrize("failure", [SoftTimeLimitExceeded("time is up"),
                                     RuntimeError("SSL SYSCALL error: EOF detected")])
def test_a_failed_end_of_transaction_rolls_back_but_a_time_limit_propagates(failure):
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        s.execute(text("SELECT 1"))

        def _commit():
            raise failure

        s.commit = _commit
        if isinstance(failure, SoftTimeLimitExceeded):
            with pytest.raises(SoftTimeLimitExceeded):
                enrich._end_transaction_before_network(s, "job")
        else:
            enrich._end_transaction_before_network(s, "job")   # logged; the lookup still runs
            assert not s.in_transaction()


def test_the_helper_ends_a_transaction_a_read_after_rollback_opened():
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        s.execute(text("SELECT 1"))
        s.rollback()
        assert not s.in_transaction()
        enrich._end_transaction_before_network(s, "job")     # nothing open: a no-op
        assert not s.in_transaction()
        s.execute(text("SELECT 1"))                           # what an expired row's reload does
        assert s.in_transaction()
        enrich._end_transaction_before_network(s, "job")
        assert not s.in_transaction()


async def test_after_a_failed_gis_batch_the_next_batch_holds_no_transaction(
        db, business_user, redis_client, monkeypatch):
    job_id, token, _pids = await _job(db, business_user, parcels=6)
    _batch(monkeypatch)
    idle_at_call: list[int] = []

    def _per_call(i, batch, stats):
        idle_at_call.append(_idle_in_transaction())
        out = _all_answered(i, batch, stats)
        if i == 0:
            out[batch[0]]["property_address"] = "12 MAIN\x00 ST"   # its commit is refused
        return out

    calls = _county(monkeypatch, _per_call)
    await db.commit()
    await _enrich(job_id, redis_client, token)

    assert len(calls) == 3
    assert idle_at_call[1:] == [0, 0]        # the batches after the failed one


async def test_after_a_failed_ordering_read_the_name_pass_holds_no_transaction(
        db, business_user, redis_client, monkeypatch):
    from src.db.session import sync_engine

    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=business_user.id, name="island names",
        county="island", state="WA", record_type="probate", fields=["party_name"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = await _plain_job(db, business_user, config)
    for name in ("FOUND ANN", "MISS BOB"):
        await _row(db, job_id, business_user.id, party_name=name, doc_type="CERTIFICATE OF DEATH",
                   dedup_hash=f"hash-{name}", enrichment_data={"instrument_number": "1"},
                   skip_trace_status="not_attempted")
    idle_at_call: list[int] = []

    def _scripted(url, names, **_kw):
        idle_at_call.append(_idle_in_transaction())
        return [(pacs.LOOKUP_NO_MATCH, None) for _ in names]

    def _fail_ordering(conn, cursor, statement, *_a):
        if "max(created_at)" in statement and "pacs_name_lookup" in statement:
            raise RuntimeError("ordering read failed at the cursor")

    monkeypatch.setattr(pacs, "batch_lookup_pacs_by_name", _scripted)
    event.listen(sync_engine, "before_cursor_execute", _fail_ordering)
    try:
        await db.commit()
        await _enrich(job_id, redis_client, None)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _fail_ordering)

    assert idle_at_call == [0]


# ── King helpers, driven directly on rows a rollback expired ─────────────────────

async def _king_rows(db, user, n: int = 1) -> tuple[str, list[str]]:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="king recovery", county="king", state="WA",
        record_type="probate", fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = await _plain_job(db, user, config)
    rids = [await _row(db, job_id, user.id, parcel_id=f"{1234567890 + i}") for i in range(n)]
    return job_id, rids


def _expired_rows(s, job_id: str) -> list:
    rows = s.execute(select(Result).where(Result.job_id == job_id)).scalars().all()
    s.rollback()                      # what a failed commit earlier in the job does
    assert not s.in_transaction()
    return rows


async def test_the_condo_extract_refresh_holds_no_transaction(db, business_user, monkeypatch):
    from src.db.session import system_sync_session
    from src.scrapers.enrichment import king_condo_units

    job_id, _rids = await _king_rows(db, business_user)
    seen: list[bool] = []

    with system_sync_session() as s:
        monkeypatch.setattr(king_condo_units, "resolve_units",
                            lambda pins: seen.append(s.in_transaction()) or None)
        assert enrich._fill_king_condo_unit_situs(s, _expired_rows(s, job_id), job_id) == 0

    assert seen == [False]


async def test_the_not_carried_confirmation_holds_no_transaction(db, business_user, monkeypatch):
    from types import SimpleNamespace

    from src.db.session import system_sync_session
    from src.scrapers.enrichment import king_county_assessor

    job_id, _rids = await _king_rows(db, business_user)
    pin = "1234567890"
    seen: list[bool] = []
    extract = {"answers": {pin: SimpleNamespace(status="absent")}, "snapshot": "snap"}

    with system_sync_session() as s:
        monkeypatch.setattr(king_county_assessor, "confirm_not_carried",
                            lambda pins, answers: seen.append(s.in_transaction()) or set())
        settled = enrich._settle_king_not_carried(
            s, _expired_rows(s, job_id), set(), {pin}, extract, lambda: 600.0, job_id,
            str(business_user.id))

    assert settled == set()
    assert seen == [False]
