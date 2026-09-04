"""The weekly-PDF sources must not be able to lose an issue again.

The legals page exposes ONLY the current issue and links no archive, so the old
Thursday-only beat entry was a single point of failure: one missed or failed Thursday
lost that week's notices permanently. Measured for King on 2026-09-03 — 4 of 14
published issues ingested, 8 still-live auctions missing from the product.

These pin the two halves of the fix: the beat now runs daily, and the recovery script's
URL templates match the filenames the papers actually publish (verified against 14 live
King issues and 9 live Snohomish issues).
"""
import importlib.util
from datetime import date
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_nts_pdf_archive.py"


def _backfill_module():
    spec = importlib.util.spec_from_file_location("backfill_nts_pdf_archive", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Beat cadence ─────────────────────────────────────────────────────────────────

def test_pdf_crawls_run_daily_not_weekly():
    """A weekly cadence + a no-archive source = one bad day loses a whole issue."""
    from src.workers.scheduler import app

    for name in ("crawl-nts-snoho-tribune", "crawl-nts-king-queenanne"):
        entry = app.conf.beat_schedule[name]
        dow = entry["schedule"].day_of_week
        # celery normalizes "*" to the full set; a weekly entry would be a single day.
        assert len(dow) == 7, f"{name} is not daily: day_of_week={dow}"


def test_pdf_crawls_keep_distinct_minutes():
    """Staggered so two PDF downloads never start in the same minute."""
    from src.workers.scheduler import app

    snoho = app.conf.beat_schedule["crawl-nts-snoho-tribune"]["schedule"]
    king = app.conf.beat_schedule["crawl-nts-king-queenanne"]["schedule"]
    assert snoho.minute != king.minute


def test_matcher_still_runs_after_the_crawls():
    """The matcher must see a warm cache — it runs at 11:00, the crawls at 10:4x/10:5x."""
    from src.workers.scheduler import app

    matcher = app.conf.beat_schedule["match-nts-notices"]["schedule"]
    king = app.conf.beat_schedule["crawl-nts-king-queenanne"]["schedule"]
    assert min(matcher.hour) * 60 + min(matcher.minute) > min(king.hour) * 60 + min(king.minute)


# ── Recovery-script URL templates (verified against the live CDN) ─────────────────

def test_king_filename_template_matches_published_issues():
    m = _backfill_module()
    assert m._king_names(date(2026, 8, 5)) == ["QA Legals 08-05-26.pdf"]
    assert m._king_names(date(2026, 9, 2)) == ["QA Legals 09-02-26.pdf"]
    # zero-padded month AND day — an unpadded guess 404s on this paper
    assert m._king_names(date(2026, 6, 3)) == ["QA Legals 06-03-26.pdf"]


def test_snohomish_filename_template_is_unpadded_and_tries_both():
    m = _backfill_module()
    names = m._snoho_names(date(2026, 8, 5))
    assert names[0] == "Legals - 8-5-26.pdf"      # the shape the paper actually uses
    assert names[1] == "Legals - 08-05-26.pdf"    # cheap fallback; the paper is inconsistent
    assert m._snoho_names(date(2026, 7, 15))[0] == "Legals - 7-15-26.pdf"


def test_every_source_is_paired_with_its_own_parser():
    """King under the shared colon parser yields garbage — and a garbage row is worse
    than no row, because it upserts over the cache under a real TS number."""
    from src.scrapers.sources.nts_king_pdf import parse_king_notice
    from src.scrapers.sources.nts_tacoma_index import parse_nts_notice

    m = _backfill_module()
    srcs = m._sources()
    assert srcs["queen_anne_news"]["parse"] is parse_king_notice
    assert srcs["queen_anne_news"]["county"] == "king"
    assert srcs["snohomish_tribune"]["parse"] is parse_nts_notice
    assert srcs["snohomish_tribune"]["county"] == "snohomish"


def test_backfill_is_bounded():
    """A one-time recovery must not be able to hammer the CDN or run unbounded."""
    m = _backfill_module()
    assert m._MAX_ISSUES <= 60
    assert m._FETCH_DELAY_S > 0
    # Reuses the crawler's own byte cap rather than inventing a second limit.
    from src.workers.nts_crawler import _MAX_PDF_BYTES

    assert m._MAX_PDF_BYTES == _MAX_PDF_BYTES
