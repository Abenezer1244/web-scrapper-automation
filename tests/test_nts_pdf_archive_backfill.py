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
    from src.scrapers.sources.nts_pdf_archive import king_names

    assert king_names(date(2026, 8, 5)) == ["QA Legals 08-05-26.pdf"]
    assert king_names(date(2026, 9, 2)) == ["QA Legals 09-02-26.pdf"]
    # zero-padded month AND day — an unpadded guess 404s on this paper
    assert king_names(date(2026, 6, 3)) == ["QA Legals 06-03-26.pdf"]


def test_snohomish_filename_templates_cover_both_observed_spellings():
    """The paper renamed its own files mid-catalogue, so both spellings must be probed.

    "Legals - 8-5-26.pdf" is the back catalogue (6-10, 6-17, 6-24, 7-22, 8-5, 8-27 all
    verified live). By 2026-09-02 it had dropped the separator — "Legals 9-2-26.pdf",
    confirmed HTTP 200 while the separator form 404s — and that variant was MISSING, so
    the newest Snohomish issues were unreachable by construction.
    """
    from src.scrapers.sources.nts_pdf_archive import snoho_names

    names = snoho_names(date(2026, 8, 5))
    assert names[0] == "Legals - 8-5-26.pdf"
    assert "Legals 8-5-26.pdf" in names          # the 2026-09 spelling
    assert "Legals - 08-05-26.pdf" in names      # cheap padded fallbacks
    assert "Legals 08-05-26.pdf" in names
    assert snoho_names(date(2026, 9, 2))[1] == "Legals 9-2-26.pdf"


def test_backfill_script_and_beat_sweep_share_one_archive_map():
    """The one-shot recovery and the daily self-heal must not disagree about where a
    back issue lives — they did, which is how the Snohomish rename went unnoticed."""
    from src.scrapers.sources.nts_pdf_archive import ARCHIVE_SOURCES

    m = _backfill_module()
    script = m._sources()
    assert set(script) == set(ARCHIVE_SOURCES)
    for name, cfg in ARCHIVE_SOURCES.items():
        assert script[name]["names"] is cfg["names"]
        assert script[name]["prefix"] == cfg["prefix"]
        assert script[name]["county"] == cfg["county"]


def test_recent_issue_dates_are_oldest_first_and_weekday_anchored():
    """Oldest-first is load-bearing: the upsert refreshes every mutable field, so the
    NEWEST issue must write last or a postponed sale reverts to its old date."""
    from src.scrapers.sources.nts_pdf_archive import recent_issue_dates

    for anchor in (date(2026, 9, 2), date(2026, 9, 4), date(2026, 9, 8)):
        got = recent_issue_dates("queen_anne_news", anchor, weeks=3)
        assert got == sorted(got), "issue dates must be oldest first"
        assert all(d.weekday() == 2 for d in got), "issues are dated Wednesday"
        assert got[-1] <= anchor, "never derives an issue from the future"
    # a Friday anchor still resolves to that week's Wednesday, not the next one
    assert recent_issue_dates("queen_anne_news", date(2026, 9, 4), weeks=1) == [
        date(2026, 9, 2)
    ]


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
