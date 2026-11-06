"""A back issue swept after a newer one cannot put the newer one's values back.

Codex 2026-11-06: the weekly-PDF crawl ingests the current issue, then sweeps back
issues, and both upsert on (source, ts_number). A sale postponed in the newer issue
reverted to its original date when an older issue was swept, and the matched-lead
refresh would then carry that regression onto leads. Real test DB, real upsert SQL.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from src.db.models import NtsNotice
from src.db.session import SyncSessionLocal
from src.scrapers.sources.nts_pdf_archive import issue_date_from_url, issue_url, king_names
from src.workers.nts_crawler import _upsert_notice

_SOURCE = "queen_anne_news"


def _row(ts: str, issue: date | None, **values) -> dict:
    url = issue_url(_SOURCE, king_names(issue)[0]) if issue else "https://example.test/notice"
    return {
        "source": _SOURCE, "ts_number": ts, "county": "king", "state": "WA",
        # No issue_date: _upsert_notice derives it, as the operator backfill relies on.
        "source_url": url, "is_active": True, "fetched_at": datetime.now(UTC),
        **values,
    }


def _upsert(row: dict) -> NtsNotice:
    with SyncSessionLocal() as db:
        _upsert_notice(db, NtsNotice, row)
        db.commit()
        return db.execute(
            select(NtsNotice).where(NtsNotice.source == _SOURCE,
                                    NtsNotice.ts_number == row["ts_number"])
        ).scalar_one()


def test_an_older_issue_only_fills_gaps_and_a_newer_one_still_wins():
    ts = f"TEST-ISSUE-{uuid.uuid4().hex[:8]}"
    original = date.today() + timedelta(days=12)
    postponed = date.today() + timedelta(days=40)
    moved_again = date.today() + timedelta(days=55)
    try:
        cur = _upsert(_row(ts, date(2026, 9, 30), auction_date=postponed,
                           principal_owing=Decimal("491002.57")))
        assert cur.issue_date == date(2026, 9, 30)

        # The back issue from three weeks earlier: original date, old amount, and a
        # trustee the newer issue's parse missed.
        old = _upsert(_row(ts, date(2026, 9, 9), auction_date=original,
                           principal_owing=Decimal("456249.92"), trustee="OLD ISSUE TRUSTEE"))
        assert old.auction_date == postponed
        assert old.principal_owing == Decimal("491002.57")
        assert old.trustee == "OLD ISSUE TRUSTEE"       # a gap may still be filled
        assert old.issue_date == date(2026, 9, 30)
        assert old.source_url.endswith("09-09-26.pdf")  # the sweep's due-check still moves

        new = _upsert(_row(ts, date(2026, 10, 7), auction_date=moved_again,
                           principal_owing=Decimal("495000.00")))
        assert new.auction_date == moved_again
        assert new.principal_owing == Decimal("495000.00")
        assert new.issue_date == date(2026, 10, 7)
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(NtsNotice).where(NtsNotice.ts_number == ts))
            db.commit()


def test_a_source_without_issue_dates_keeps_newest_read_wins():
    ts = f"TEST-HTML-{uuid.uuid4().hex[:8]}"
    first = date.today() + timedelta(days=20)
    second = date.today() + timedelta(days=30)
    try:
        _upsert(_row(ts, None, auction_date=first))
        again = _upsert(_row(ts, None, auction_date=second))
        assert again.auction_date == second
        assert again.issue_date is None
    finally:
        with SyncSessionLocal() as db:
            db.execute(delete(NtsNotice).where(NtsNotice.ts_number == ts))
            db.commit()


def test_migration_127_backfill_parses_like_the_crawler():
    """The migration stamps legacy rows with its own copy of the parse (migrations do
    not import app code); the two must agree, or legacy rows get the wrong version."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "m127", Path(__file__).resolve().parents[1] / "alembic/versions/127_nts_notice_issue_date.py")
    m127 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m127)
    from src.scrapers.sources.nts_pdf_archive import snoho_names

    urls = [issue_url(_SOURCE, n) for n in king_names(date(2026, 9, 2))]
    urls += [issue_url("snohomish_tribune", n) for n in snoho_names(date(2026, 8, 27))]
    urls += ["https://www.tacomadailyindex.com/legal-notices/x", None, "https://h/Legals%2013-40-26.pdf"]
    for u in urls:
        assert m127._issue_date(u) == issue_date_from_url(u)

