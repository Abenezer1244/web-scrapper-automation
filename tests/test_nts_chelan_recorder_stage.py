"""Chelan NTS recorder staging: what a crawl writes to nts_recorder_documents.

Real test database, real OCR child process, and in place of tesseract the stand-in
executables of test_nts_recorder_ocr (they write TSV the way tesseract does). The
detail page is Chelan's real one (fixture, names redacted). Network-free: discovery and
downloads are the crawl task's; stage_recorder_document is everything after them.
"""
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from src.db.models import NtsRecorderDocument as Doc
from src.workers.nts_crawler import (
    RECORDER_EXPIRED_NOTE,
    RECORDER_PENDING_TTL_DAYS,
    purge_stale_recorder_documents,
    stage_recorder_document,
)
from tests.test_nts_recorder_ocr import _OK, _tool, recorder_pdf

_DETAIL = (Path(__file__).parent / "fixtures" / "acclaimweb_chelan" / "detail_1788730.html").read_text(
    encoding="utf-8")
_DOC = {"item_id": "1788730", "instrument": "2627524", "doc_code": "NTS", "record_date": "09/28/2026"}


@pytest.fixture
def sync_db():
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        db.execute(text("DELETE FROM nts_recorder_documents"))
        db.commit()
        yield db
        db.rollback()
        db.execute(text("DELETE FROM nts_recorder_documents"))
        db.commit()


def _row(db) -> Doc:
    return db.execute(select(Doc).where(Doc.transaction_item_id == "1788730")).scalar_one()


def test_a_read_notice_is_staged_for_review_with_the_recordings_own_facts(sync_db, tmp_path):
    status = stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                                     detail_html=_DETAIL, pdf=recorder_pdf(2),
                                     tesseract=_tool(tmp_path, "tess_ok", _OK))
    sync_db.commit()
    row = _row(sync_db)
    assert status == row.status == "pending_review"
    assert row.ocr_text  # kept only while awaiting review
    assert row.recording_apn == "222014846080" and row.related_deed_of_trust == "2531058"
    assert row.record_date == date(2026, 9, 28) and row.page_count == 2 and len(row.pdf_sha256) == 64
    assert "grantor" not in row.fields["fields"]  # the borrower never enters stored fields
    assert row.fields["recording_apns"] == ["222014846080"]
    # The stand-in OCR read no parcel: the reviewer is told, and nothing is invented.
    assert "parcel: not read from the notice" in row.fields["flags"]


def test_an_unreadable_document_is_staged_failed_with_its_reason_and_no_text(sync_db, tmp_path):
    status = stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                                     detail_html=_DETAIL, pdf=b"<html>not a pdf</html>",
                                     tesseract=_tool(tmp_path, "tess_ok", _OK))
    sync_db.commit()
    row = _row(sync_db)
    assert (status, row.status, row.decision_note, row.ocr_text) == ("failed", "failed", "not a pdf", None)


def test_a_failed_row_is_retried_but_a_row_awaiting_review_is_kept(sync_db, tmp_path):
    tool = _tool(tmp_path, "tess_ok", _OK)
    stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                            detail_html=_DETAIL, pdf=None, tesseract=tool)
    sync_db.commit()
    assert _row(sync_db).status == "failed"
    assert stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                                   detail_html=_DETAIL, pdf=recorder_pdf(1),
                                   tesseract=tool) == "pending_review"
    sync_db.commit()
    first_text = _row(sync_db).ocr_text
    # A later read never replaces what is awaiting (or has had) an operator's review.
    assert stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                                   detail_html=_DETAIL, pdf=recorder_pdf(3),
                                   tesseract=tool) == "kept"
    sync_db.commit()
    sync_db.expire_all()
    assert _row(sync_db).page_count == 1 and _row(sync_db).ocr_text == first_text


def test_unreviewed_rows_expire_losing_their_text_and_are_not_reread(sync_db, tmp_path):
    tool = _tool(tmp_path, "tess_ok", _OK)
    stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                            detail_html=_DETAIL, pdf=recorder_pdf(1), tesseract=tool)
    fresh = {**_DOC, "item_id": "1790252", "instrument": "2628287"}
    stage_recorder_document(sync_db, county="chelan", state="WA", doc=fresh,
                            detail_html=_DETAIL, pdf=recorder_pdf(1), tesseract=tool)
    sync_db.execute(text("UPDATE nts_recorder_documents SET created_at = :old "
                         "WHERE transaction_item_id = '1788730'"),
                    {"old": datetime.now(UTC) - timedelta(days=RECORDER_PENDING_TTL_DAYS + 1)})
    sync_db.commit()

    assert purge_stale_recorder_documents(sync_db) == 1
    sync_db.commit()
    sync_db.expire_all()
    old = _row(sync_db)
    assert (old.status, old.ocr_text, old.decision_note) == ("failed", None, RECORDER_EXPIRED_NOTE)
    other = sync_db.execute(select(Doc).where(Doc.transaction_item_id == "1790252")).scalar_one()
    assert other.status == "pending_review" and other.ocr_text  # within its time: untouched
    # An expired row is not read again (the reviewer's window passed; no endless re-OCR).
    assert stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC,
                                   detail_html=_DETAIL, pdf=recorder_pdf(1), tesseract=tool) == "kept"


def test_expiry_also_cuts_the_stored_fields_to_the_recordings_own_facts(sync_db, tmp_path):
    stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC, detail_html=_DETAIL,
                            pdf=recorder_pdf(1), tesseract=_tool(tmp_path, "tess_ok", _OK))
    sync_db.execute(text("UPDATE nts_recorder_documents SET created_at = now() - interval '40 days'"))
    sync_db.commit()
    purge_stale_recorder_documents(sync_db)
    sync_db.commit()
    sync_db.expire_all()
    assert set(_row(sync_db).fields) == {"recording_apns", "related_deeds_of_trust"}
    assert _row(sync_db).fields["recording_apns"] == ["222014846080"]


def test_the_scheduled_purge_runs_without_any_crawl(sync_db, tmp_path):
    from src.workers.nts_crawler import purge_nts_recorder_staging

    stage_recorder_document(sync_db, county="chelan", state="WA", doc=_DOC, detail_html=_DETAIL,
                            pdf=recorder_pdf(1), tesseract=_tool(tmp_path, "tess_ok", _OK))
    sync_db.execute(text("UPDATE nts_recorder_documents SET created_at = now() - interval '40 days'"))
    sync_db.commit()
    assert purge_nts_recorder_staging() == 1
    sync_db.expire_all()
    assert _row(sync_db).ocr_text is None


def test_the_purge_is_on_the_beat_schedule():
    from src.workers.scheduler import app as beat_app

    entry = beat_app.conf.beat_schedule["purge-nts-recorder-staging"]
    assert entry["task"] == "src.workers.nts_crawler.purge_nts_recorder_staging"


@pytest.mark.parametrize("item_id,ok", [
    ("1788730", True), ("", False), ("12a", False), ("１２３", False),  # full-width digits
    ("1" * 33, False), ("1/../2", False),
])
def test_only_plain_short_numeric_document_ids_are_fetched(item_id, ok):
    from src.workers.nts_crawler import _ITEM_ID_RE

    assert bool(_ITEM_ID_RE.fullmatch(item_id)) is ok


def test_the_lock_is_released_only_by_its_holder(redis_client):
    import redis as sync_redis

    from src.config import settings
    from src.workers.nts_crawler import _CHELAN_LOCK_KEY, _RELEASE_LOCK_LUA

    rds = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    try:
        rds.set(_CHELAN_LOCK_KEY, "successor-token")
        assert rds.eval(_RELEASE_LOCK_LUA, 1, _CHELAN_LOCK_KEY, "our-expired-token") == 0
        assert rds.get(_CHELAN_LOCK_KEY) in (b"successor-token", "successor-token")
        assert rds.eval(_RELEASE_LOCK_LUA, 1, _CHELAN_LOCK_KEY, "successor-token") == 1
        assert rds.get(_CHELAN_LOCK_KEY) is None
    finally:
        rds.delete(_CHELAN_LOCK_KEY)
        rds.close()


def test_a_slowly_dripping_download_is_stopped_at_its_wall_clock_deadline(tmp_path):
    # Every chunk arrives inside the per-read timeout, so only a total deadline stops it.
    import time

    from src.utils.safe_http import _stream_capped

    def drip():
        while True:
            time.sleep(0.05)
            yield b"x"

    started = time.monotonic()
    with pytest.raises(ValueError, match="time limit"):
        _stream_capped(drip(), str(tmp_path / "out"), max_bytes=10**9,
                       deadline=time.monotonic() + 0.5)
    assert time.monotonic() - started < 5


def test_without_a_deadline_downloads_are_unchanged(tmp_path):
    from src.utils.safe_http import _stream_capped

    assert _stream_capped(iter([b"ab", b"", b"cd"]), str(tmp_path / "out"), max_bytes=10) == 4


def test_a_real_slow_server_is_cut_off_at_the_deadline_not_a_chunk_later(tmp_path):
    # A real local HTTP server declares 1000 bytes and sends one every 0.2 s: each read
    # is well inside the 5 s read timeout, and a 64 KiB chunk would wait the whole
    # 200 s for the body. The deadline must stop it within about a second.
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import requests

    from src.utils.safe_http import _read1_chunks, _stream_capped

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(1000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = requests.get(f"http://127.0.0.1:{server.server_port}/", stream=True, timeout=5)
        started = time.monotonic()
        with pytest.raises(ValueError, match="time limit"):
            _stream_capped(_read1_chunks(resp, 65536), str(tmp_path / "out"), max_bytes=10**6,
                           deadline=time.monotonic() + 1.0)
        assert time.monotonic() - started < 3
        resp.close()
    finally:
        server.shutdown()
        server.server_close()
