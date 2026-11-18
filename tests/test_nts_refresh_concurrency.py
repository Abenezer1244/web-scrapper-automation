"""An address refresh and a trustee-sale read cannot interleave (#650, Codex P1/P3).

Two real sessions on the real test database. refresh_notice_address holds
NTS_CANCEL_LOCK_KEY exclusively until it commits; the real ChelanWATrusteeSaleScraper
reads notices under the same key, shared. So a read that starts during a refresh waits
for it and then sees the new address, never the old one, and the scraper's shared lock
is gone once its read is done (it must not hold the key through the slow part of a job).
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session
from src.scrapers.trustee_sale import ChelanWATrusteeSaleScraper
from src.workers.nts_crawler import refresh_notice_address
from src.workers.nts_matcher_task import NTS_CANCEL_LOCK_KEY

_PRINTED = "170 HILL STREET, MANSON, WA 98831"
_SITUS = "62 VILLAGE VISTA LN MANSON, WA 98831"
_PARCEL = "282135608610"


@pytest.fixture
def approved_notice():
    """One approved Chelan notice and its staging row, committed (the scraper reads in
    its own session), with a fresh crawl heartbeat; all removed afterwards."""
    from src.scrapers.enrichment.source_health import mark_source_healthy

    def _clean(db):
        db.execute(text("DELETE FROM nts_recorder_documents WHERE county = 'chelan'"))
        db.execute(text("DELETE FROM nts_notices WHERE county = 'chelan'"))
        db.execute(text("DELETE FROM external_source_health WHERE source_key = 'nts_crawl:chelan'"))
        db.commit()

    notice_id, doc_id = str(uuid.uuid4()), str(uuid.uuid4())
    with system_sync_session() as db:
        _clean(db)
        db.execute(text(
            "INSERT INTO nts_notices (id, source, ts_number, county, state, parcel, property_address, "
            "property_address_normalized, auction_date, grantor, is_active, fetched_at, address_source, "
            "address_on_notice) VALUES (:n, 'chelan_recorder', 'REC-2699001', 'chelan', 'WA', :p, :a, "
            "'170 HILL ST MANSON|98831', :d, 'DOE, JANE', true, now(), 'notice', :a)"),
            {"n": notice_id, "p": _PARCEL, "a": _PRINTED, "d": date.today() + timedelta(days=60)})
        db.execute(text(
            "INSERT INTO nts_recorder_documents (id, county, state, transaction_item_id, instrument_number, "
            "doc_code, record_date, status, decision_note, reviewed_by, reviewed_at, nts_notice_id, "
            "created_at, updated_at) VALUES (:i, 'chelan', 'WA', '9990001', '2699001', 'NTS', :rd, "
            "'approved', NULL, 'op', now(), :n, now(), now())"),
            {"i": doc_id, "n": notice_id, "rd": date.today()})
        mark_source_healthy(db, "nts_crawl:chelan")
        db.commit()
        yield doc_id
        db.rollback()
        _clean(db)


def _scrape():
    today = datetime.now(UTC).strftime("%m/%d/%Y")
    return asyncio.run(ChelanWATrusteeSaleScraper().scrape(today, today))


def _reader_is_waiting_on_the_lock(deadline_s: float = 15.0) -> bool:
    """True once a session is queued (not granted) on NTS_CANCEL_LOCK_KEY. Watching
    pg_locks, not a sleep, so a reader that has not reached its read yet cannot pass for
    a blocked one (Codex)."""
    hi, lo = (NTS_CANCEL_LOCK_KEY >> 32) & 0xFFFFFFFF, NTS_CANCEL_LOCK_KEY & 0xFFFFFFFF
    end = time.monotonic() + deadline_s
    with system_sync_session() as probe:
        while time.monotonic() < end:
            waiting = probe.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                "AND classid = CAST(:hi AS oid) AND objid = CAST(:lo AS oid) AND objsubid = 1"),
                {"hi": hi, "lo": lo}).scalar()
            probe.rollback()
            if waiting:
                return True
            time.sleep(0.1)
    return False


def test_a_read_during_a_refresh_waits_and_sees_the_new_address(approved_notice):
    out: dict = {}

    def _reader():
        try:
            out["records"] = _scrape()
        except BaseException as exc:  # noqa: BLE001 -- surfaced by the assertion below
            out["error"] = exc

    reader = threading.Thread(target=_reader, daemon=True)
    with system_sync_session() as refresh:
        try:
            assert refresh_notice_address(refresh, approved_notice, reviewer="op",
                                          situs=_SITUS) == (_PRINTED, _SITUS)
            reader.start()
            waited = _reader_is_waiting_on_the_lock()
            refresh.commit()
        finally:
            refresh.rollback()  # no-op after the commit; releases the lock on any failure
            if reader.ident is not None:  # started: always wait for it, pass or fail
                reader.join(timeout=30)
    assert not reader.is_alive(), "the reader did not finish"
    assert waited, "the read must wait for the uncommitted refresh"
    assert "error" not in out, out.get("error")
    (lead,) = out["records"]
    assert lead.property_address == _SITUS
    assert lead.enrichment_data["nts_source"]["address_on_notice"] == _PRINTED


def test_the_scrapers_shared_lock_is_released_once_its_read_is_done(approved_notice):
    assert len(_scrape()) == 1
    with system_sync_session() as db:
        # Exclusive and non-blocking: true only if no reader still holds the key shared.
        assert db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": NTS_CANCEL_LOCK_KEY}).scalar()
        db.rollback()
