"""NTS Tier 1 crawler: harvest Pierce trustee-sale notices into nts_notices.

A Celery beat task (daily). Crawls the Tacoma Daily Index legal-notices listing,
fetches + parses each Notice-of-Trustee-Sale via the SSRF-guarded safe_get, and
upserts into the nts_notices shared cache (system role, FOR ALL policy). The
matcher (NTS-3) later attaches the auction fields onto each tenant's
pre_foreclosure Results — this task only maintains the cache.

Legal/operational: the source's robots.txt is fully open; we still rate-limit
between fetches, cap pages/notices per run, cache ~90 days, and flip is_active
False once an auction is in the past (kept for audit, excluded from matching).
Decoupling the slow crawl from user scrape jobs is deliberate (Codex).
"""
from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, false, null
from sqlalchemy import text as _sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.utils.logger import setup_logger
from src.workers import app

_logger = setup_logger("workers.nts_crawler")

_MAX_PAGES = 10           # listing pages per run (newest first). NTS are a MINORITY of
                          # the mixed legal-notices feed, so we must scan several pages
                          # to reach the trustee sales behind the day's probate/bids.
_MAX_NOTICES = 200        # hard cap on fetches per run
_FETCH_DELAY_S = 1.0      # polite delay between notice fetches
_LISTING_DELAY_S = 0.5    # polite delay between listing-page fetches (we now always
                          # walk multiple pages per run — Bug A fix)
_CACHE_DAYS = 90          # expire notices not seen in this window
# Re-sweep (2026-09-02): the listing pass only reaches the newest _MAX_PAGES pages, so a
# parser fix never revisited an older notice — a 07/31 notice sat with a NULL amount
# for a month. Each URL-based crawl (Tacoma, Clark) now re-fetches a bounded batch of
# still-active, future-dated notices that carry NO amount and were last fetched over
# _RESWEEP_MIN_AGE_HOURS ago, so a genuinely amount-less notice costs one fetch/day.
_RESWEEP_LIMIT = 25
_RESWEEP_MIN_AGE_HOURS = 20

# ── Pacific Publishing weekly-PDF crawlers (Snohomish Tribune, Queen Anne News) ──
# These papers publish one weekly "Legals" PDF carrying MANY notices (vs Tacoma's
# one HTML page per notice). The PDF filename/CDN domain are unstable, so we DISCOVER
# the current PDF by scraping the paper's legal-notices page and taking the newest
# CDN link whose filename contains "legal" (the page also lists a "CLASS …" classifieds
# PDF we must NOT pick). The legal-notices page soft-404s (returns 404 with the links
# present), so discovery parses the body regardless of status. The discovery page gates
# non-browser UAs with a 403, so we send a browser UA (robots.txt allows "*"; these are
# public statutory RCW 61.24.040 notices). The CDN PDF download itself needs no special UA.
_PDF_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
_MAX_PDF_BYTES = 25 * 1024 * 1024   # a weekly legals PDF is < 1 MB; cap hostile inputs
_PDF_HOST = "pacificpublishingcompany.media.clients.ellingtoncms.com"

_SNOHO_PAGE = "https://www.snoho.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498"
_SNOHO_PDF_PREFIX = "/static-4/snoho/images/"
# source is the nts_notices natural-key prefix and is varchar(32) — keep it short.
_SNOHO_SOURCE = "snohomish_tribune"

# King County via the Queen Anne & Magnolia News (Pacific Publishing). PARTIAL
# coverage — it's a neighborhood paper, not King County's dominant foreclosure venue
# (that's the Seattle DJC). Same weekly-PDF pipeline; its legals live in a /legals/
# subdir (vs snoho's flat /images/).
# DJC status, scoped 2026-09-03 (docs/scoping-king-nts-coverage-2026-09-03.md): NOT a
# "just pay for it" item. The old "$350/yr" note here was stale — the relevant tier is
# $199/yr — but price was never the blocker: djc.com/robots.txt disallows /notices/ for
# ALL crawlers, and the free WNPA aggregator carrying the same notices
# (wapublicnotices.com) bans scraping outright in its Terms of Use. A subscription buys
# reading access, not crawl permission. The cheap path to more King coverage is the
# OTHER approved King legal newspapers, not the DJC.
_KING_PAGE = "https://queenannenews.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498"
_KING_PDF_PREFIX = "/static-4/queenannenews/images/legals/"
_KING_SOURCE = "queen_anne_news"

# ── Weekly-archive sweep ─────────────────────────────────────────────────────
# The legal-notices page exposes ONLY the current issue and carries no archive link, so
# every prior design treated a missed/failed week as PERMANENTLY lost — going daily
# (2026-09-03) narrowed the window but still cannot recover a week already gone, and
# scripts/backfill_nts_pdf_archive.py can only recover one when an operator remembers to
# run it (it never was: King sat 4 weeks stale until the 2026-09-04 Test 8 audit).
#
# So the beat now heals itself. The back issues are unlinked but public and addressable
# by derived URL — the filename is the issue date — and src/scrapers/sources/
# nts_pdf_archive.py owns that mapping for both the operator script and this sweep, so
# the two cannot drift.
#
# Bounded on purpose: only the recent ISSUE WEEKDAYS (not every date, the way the
# operator backfill probes), only issues we have not read lately, oldest first, capped
# at _ARCHIVE_MAX_FETCH downloads per run with the same polite delay as every other
# fetch. Steady state is 0-1 fetches/day.
_ARCHIVE_WEEKS = 8            # how far back a sweep will reach
_ARCHIVE_MAX_FETCH = 6        # hard cap on archive downloads per run
_ARCHIVE_REFRESH_DAYS = 7     # re-read an issue whose newest row is older than this

# ── The Columbian classifieds (Clark County) ──
# Clark County trustee sales publish free/open (robots.txt 404) on The Columbian's
# classifieds site — a single rolling HTML listing (no pagination) mixing every current
# legal notice. Ingestion adapter: src/scrapers/sources/nts_columbian.py. Unlike Tacoma
# (one NTS-slug page per notice) we fetch EVERY ad permalink and let is_valid_nts
# backstop — the listing preview is truncated, so pre-filtering could silently drop a
# lead (Codex Q1). Browser UA (some classifieds gate non-browser UAs); these are public
# statutory RCW 61.24.040 notices.
_CLARK_MAX_ADS = 60  # hard cap on ad-detail fetches per run (the listing is ~32)

# ── Lewiston Tribune on nwmarket.com (Asotin County) ──
# ~15 new adverts/day, two requests each at the 20 s robots.txt delay. The window heals
# a missed run or a lagging sitemap; the Redis memo keeps each advert to one read.
_NWM_WINDOW_DAYS = 7
_NWM_MAX_ADVERTS = 60          # per run; the rest wait for the next run, logged
_NWM_DEADLINE_S = 45 * 60      # under the 55 min soft limit
_NWM_SEEN_TTL_S = 14 * 86400   # outlives the window
_NWM_MIN_TEXT_CHARS = 100      # below this a PDF is unreadable (scan); real notices are 800+


@app.task(name="src.workers.nts_crawler.crawl_nts_tacoma_index")
def crawl_nts_tacoma_index() -> dict:
    """Crawl + upsert recent Tacoma Daily Index NTS notices. Returns a summary."""
    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_get

    today = datetime.now(UTC).date()
    listing_pages_read = 0

    def _fetch_listing(page: int):
        """I/O for one listing page: returns (status_code, html) or None on failure.

        A transient failure returns None so collect_notice_urls skips just this page
        (a later page may still carry trustee sales) instead of abandoning the crawl.
        """
        nonlocal listing_pages_read
        if page > 1:
            time.sleep(_LISTING_DELAY_S)  # polite: we now walk multiple pages every run
        url = nts.BASE_URL + nts.LEGAL_NOTICES_PATH + (f"page/{page}/" if page > 1 else "")
        try:
            resp = safe_get(url, timeout=20, headers={"User-Agent": "BridgeLeadsBot/1.0"})
        except Exception as exc:  # noqa: BLE001 — a bad page must not kill the crawl
            _logger.warning("NTS listing page %d fetch failed: %s", page, str(exc)[:120])
            return None
        if resp.status_code == 200:
            listing_pages_read += 1
        return (resp.status_code, resp.text)

    notice_urls = nts.collect_notice_urls(
        _fetch_listing, max_pages=_MAX_PAGES, max_notices=_MAX_NOTICES
    )
    _logger.info("NTS crawl: %d candidate notice URLs", len(notice_urls))

    upserted = skipped = errored = 0
    _drops: dict = {}
    with system_sync_session() as db:
        for u in notice_urls:
            try:
                # same_origin_as pins the fetch to the Tacoma Daily Index origin —
                # defense-in-depth with the host-pinned URL regex (Codex P2).
                resp = safe_get(
                    u, timeout=20, same_origin_as=nts.BASE_URL,
                    headers={"User-Agent": "BridgeLeadsBot/1.0"},
                )
                if resp.status_code != 200:
                    errored += 1
                    continue
                parsed = nts.parse_tacoma_notice(nts.extract_article_text(resp.text))
                row = nts.notice_to_row(parsed, source_url=u, today=today)
                if row is None:
                    _note_undated_drop(_drops, parsed, "tacoma_daily_index")
                    skipped += 1  # not a parseable NTS body
                    continue
                row["fetched_at"] = datetime.now(UTC)
                _upsert_notice(db, NtsNotice, row, deed_reference=parsed.get("deed_reference"))
                upserted += 1
            except Exception as exc:  # noqa: BLE001
                errored += 1
                _logger.warning("NTS notice %s failed: %s", u, str(exc)[:120])
            time.sleep(_FETCH_DELAY_S)
        db.commit()

        # Re-sweep older active notices the listing pass can no longer reach.
        def _fetch_notice(u: str):
            resp = safe_get(u, timeout=20, same_origin_as=nts.BASE_URL,
                            headers={"User-Agent": "BridgeLeadsBot/1.0"})
            return resp.status_code, resp.text

        resweep = _resweep_null_amount_notices(
            db, NtsNotice, source=nts.SOURCE, county=nts.COUNTY, today=today,
            fetch=_fetch_notice,
            parse=lambda html: nts.parse_tacoma_notice(nts.extract_article_text(html)),
            notice_to_row=nts.notice_to_row,
        )
        db.commit()

        # Expire: past-auction + anything not refreshed within the cache window.
        expired = db.execute(
            _sa_text(
                """
                UPDATE nts_notices SET is_active = false
                WHERE is_active
                  AND (auction_date < :today
                       OR fetched_at < :cutoff)
                """
            ),
            {"today": today, "cutoff": datetime.now(UTC) - _td_days(_CACHE_DAYS)},
        ).rowcount
        db.commit()
        # The listing was READ (a day with no trustee sale still counts), and any
        # trustee-sale notice it listed parsed (all failing is parser drift).
        if listing_pages_read and (not notice_urls or upserted > 0):
            _record_crawl_heartbeat(db, nts.COUNTY)

    summary = {"candidates": len(notice_urls), "upserted": upserted,
               "skipped": skipped, "errored": errored, "expired": expired or 0,
               "resweep": resweep,
               "dropped_undated": _drops.get("dropped_undated", 0)}
    _logger.info("NTS crawl done: %s", summary)
    _alert_if_crawl_barren("tacoma_daily_index", discovered=len(notice_urls), upserted=upserted)
    return summary


@app.task(name="src.workers.nts_crawler.crawl_nts_snoho_tribune")
def crawl_nts_snoho_tribune() -> dict:
    """Crawl the Snohomish County Tribune weekly Legals PDF into nts_notices."""
    # The Tribune carries the same Affinia no-colon and no-TS# layouts as the King
    # paper (live 2026-09-09: an Affinia sale and a Burns Law commercial sale were
    # both dropped by the plain colon parser), so it uses the PDF parser with the REF-
    # surrogate identity (never APN-, see parse_snoho_notice). On every notice that
    # already carried its own TS number the output is unchanged (measured over 5
    # Tribune issues).
    from src.scrapers.sources.nts_king_pdf import parse_snoho_notice
    return _crawl_pacific_publishing_pdf(
        page_url=_SNOHO_PAGE,
        pdf_path_prefix=_SNOHO_PDF_PREFIX,
        source=_SNOHO_SOURCE,
        county="snohomish",
        parse_fn=parse_snoho_notice,
        archive=True,
    )


@app.task(name="src.workers.nts_crawler.crawl_nts_king_queenanne")
def crawl_nts_king_queenanne() -> dict:
    """Crawl the Queen Anne & Magnolia News weekly Legals PDF into nts_notices (King)."""
    from src.scrapers.sources.nts_king_pdf import parse_king_notice
    return _crawl_pacific_publishing_pdf(
        page_url=_KING_PAGE,
        pdf_path_prefix=_KING_PDF_PREFIX,
        source=_KING_SOURCE,
        county="king",
        parse_fn=parse_king_notice,
        archive=True,
    )


@app.task(name="src.workers.nts_crawler.crawl_nts_columbian_clark")
def crawl_nts_columbian_clark() -> dict:
    """Crawl + upsert current Clark County NTS notices from The Columbian classifieds.

    Fetches the single legal-notices listing, then EVERY /ad-details permalink on it (no
    NTS pre-filter — is_valid_nts is the backstop), parses each via the shared parser,
    and upserts the trustee sales into nts_notices. Non-NTS ads (court summons, probate,
    RFP/bids) parse to is_valid_nts False and are skipped. Returns a summary.
    """
    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_columbian as col
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_get

    today = datetime.now(UTC).date()
    listing_url = col.BASE_URL + col.LISTING_PATH
    summary = {"source": col.SOURCE, "discovered": 0, "upserted": 0,
               "skipped": 0, "errored": 0, "expired": 0}

    try:
        # same_origin_as pins the fetch to The Columbian classifieds origin (Codex P2).
        resp = safe_get(listing_url, timeout=25, same_origin_as=col.BASE_URL,
                        headers={"User-Agent": _PDF_BROWSER_UA})
    except Exception as exc:  # noqa: BLE001 — a bad listing fetch must not crash the beat
        _logger.warning("Clark NTS listing fetch failed: %s", str(exc)[:140])
        _alert_if_crawl_barren(col.SOURCE, discovered=0, upserted=0)
        return summary
    if resp.status_code != 200:
        _logger.warning("Clark NTS listing HTTP %d", resp.status_code)
        _alert_if_crawl_barren(col.SOURCE, discovered=0, upserted=0)
        return summary

    all_ad_urls = col.extract_ad_detail_urls(resp.text)
    ad_urls = all_ad_urls[:_CLARK_MAX_ADS]
    summary["discovered"] = len(ad_urls)
    if len(all_ad_urls) > _CLARK_MAX_ADS:
        # Never truncate silently (Codex P3): if the listing grows past the cap, a
        # trustee sale later in the page would be dropped unseen — log it loudly so the
        # cap can be raised. The listing is normally ~32, so this should never fire.
        _logger.warning(
            "Clark NTS listing has %d ads > cap %d — %d NOT crawled this run",
            len(all_ad_urls), _CLARK_MAX_ADS, len(all_ad_urls) - _CLARK_MAX_ADS,
        )
    _logger.info("Clark NTS crawl: %d ad permalinks", len(ad_urls))

    with system_sync_session() as db:
        for u in ad_urls:
            try:
                r = safe_get(u, timeout=20, same_origin_as=col.BASE_URL,
                             headers={"User-Agent": _PDF_BROWSER_UA})
                if r.status_code != 200:
                    summary["errored"] += 1
                    continue
                parsed = nts.parse_tacoma_notice(col.extract_ad_body(r.text))
                row = nts.notice_to_row(parsed, source_url=u, today=today,
                                        source=col.SOURCE, county=col.COUNTY)
                if row is None:
                    _note_undated_drop(summary, parsed, col.SOURCE)
                    summary["skipped"] += 1  # not an NTS (summons/probate/RFP/bid)
                    continue
                row["fetched_at"] = datetime.now(UTC)
                # Per-row SAVEPOINT: one bad notice rolls back alone, not the whole run.
                with db.begin_nested():
                    _upsert_notice(db, NtsNotice, row, deed_reference=parsed.get("deed_reference"))
                summary["upserted"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errored"] += 1
                _logger.warning("Clark NTS ad %s failed: %s", u, str(exc)[:120])
            time.sleep(_FETCH_DELAY_S)
        db.commit()

        # Re-sweep older active Clark notices that still carry no amount (URL-based
        # source, so an old permalink can be re-fetched — unlike the weekly PDFs).
        def _fetch_ad(u: str):
            r = safe_get(u, timeout=20, same_origin_as=col.BASE_URL,
                         headers={"User-Agent": _PDF_BROWSER_UA})
            return r.status_code, r.text

        summary["resweep"] = _resweep_null_amount_notices(
            db, NtsNotice, source=col.SOURCE, county=col.COUNTY, today=today,
            fetch=_fetch_ad,
            parse=lambda html: nts.parse_tacoma_notice(col.extract_ad_body(html)),
            notice_to_row=nts.notice_to_row,
        )
        db.commit()

        # SOURCE-SCOPED expiry (Codex): a Clark run must NEVER expire another source's
        # rows (do NOT copy Tacoma's global expiry). Past-auction + stale-fetch only.
        summary["expired"] = db.execute(
            _sa_text(
                """
                UPDATE nts_notices SET is_active = false
                WHERE source = :source AND is_active
                  AND (auction_date < :today OR fetched_at < :cutoff)
                """
            ),
            {"source": col.SOURCE, "today": today,
             "cutoff": datetime.now(UTC) - _td_days(_CACHE_DAYS)},
        ).rowcount or 0
        db.commit()
        # Read = the listing had ads and at least one ad detail was fetched. Most
        # Clark ads are not trustee sales, so 0 upserts is still a real read.
        if ad_urls and summary["errored"] < len(ad_urls):
            _record_crawl_heartbeat(db, col.COUNTY)

    _logger.info("Clark NTS crawl done: %s", summary)
    # Clark legitimately has 0 trustee sales on many days (the listing is mostly court
    # summons / probate / bids), so upserted==0 is NORMAL — do NOT alert on it. But DO
    # alert when the listing yielded 0 ads (discovery/extraction broke) OR when every ad
    # detail fetch failed (source down / blocked) — that 0-upsert is a real failure, not
    # a no-sale day (Codex P2). errored>=discovered means no ad was successfully read.
    _alert_if_crawl_barren(
        col.SOURCE, discovered=len(ad_urls), upserted=summary["upserted"],
        errored=summary["errored"], alert_on_zero_upserts=False,
    )
    return summary


@app.task(name="src.workers.nts_crawler.crawl_nts_nwmarket_asotin")
def crawl_nts_nwmarket_asotin() -> dict:
    """Crawl + upsert Asotin County NTS notices from the Lewiston Tribune (nwmarket.com).

    Sitemap -> adverts changed in the last _NWM_WINDOW_DAYS -> each advert page -> its PDF
    -> shared parser -> kept only when the property is in Asotin County, WA. Every request
    waits the robots.txt crawl delay. An advert is remembered in Redis only after its PDF
    was read and parsed, so a failure is retried next run and a success never re-fetched.
    """
    from urllib.parse import urlparse

    import redis as sync_redis

    from src.config import settings
    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_nwmarket as nwm
    from src.scrapers.sources import nts_pdf
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_get, safe_get_following

    today = datetime.now(UTC).date()
    deadline = time.monotonic() + _NWM_DEADLINE_S
    summary = {"source": nwm.SOURCE, "discovered": 0, "read": 0, "upserted": 0,
               "skipped": 0, "other_county": 0, "errored": 0, "expired": 0}
    headers = {"User-Agent": _PDF_BROWSER_UA}

    try:
        resp = safe_get(nwm.SITEMAP_URL, timeout=25, same_origin_as=nwm.BASE_URL, headers=headers)
    except Exception as exc:  # noqa: BLE001 — a bad sitemap fetch must not crash the beat
        _logger.warning("Asotin NTS sitemap fetch failed: %s", str(exc)[:140])
        _alert_if_crawl_barren(nwm.SOURCE, discovered=0, upserted=0)
        return summary
    if resp.status_code != 200:
        _logger.warning("Asotin NTS sitemap HTTP %d", resp.status_code)
        _alert_if_crawl_barren(nwm.SOURCE, discovered=0, upserted=0)
        return summary

    adverts = nwm.recent_adverts(resp.text, today - timedelta(days=_NWM_WINDOW_DAYS))
    summary["discovered"] = len(adverts)

    def seen_key(advert: str, version: str) -> str:
        # Keyed on the advert's lastmod too: a republished (postponed) notice is new.
        return f"bl:nts_nwmarket:seen:{advert.rsplit('_', 1)[-1]}:{version}"

    try:
        rds = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        todo = [(u, v) for u, v in adverts if not rds.exists(seen_key(u, v))]
    except Exception as exc:  # noqa: BLE001 — without the memo, read everything (slower, not wrong)
        _logger.warning("Asotin NTS seen-set unavailable, reading all: %s", str(exc)[:120])
        rds, todo = None, adverts

    attempted = 0
    with system_sync_session() as db:
        for i, (advert, version) in enumerate(todo):
            if i >= _NWM_MAX_ADVERTS or time.monotonic() > deadline:
                # Never truncate silently: the rest stay unseen and are read next run.
                _logger.warning("Asotin NTS run stopped with %d adverts unread", len(todo) - i)
                break
            attempted += 1
            try:
                time.sleep(nwm.CRAWL_DELAY_S)
                # Follows the site's case-canonicalising 301 (every hop re-validated);
                # the answer must still come from nwmarket itself.
                r = safe_get_following(advert, timeout=25, require_https=True, headers=headers)
                on_host = urlparse(r.url).hostname == urlparse(nwm.BASE_URL).hostname
                pdf_url = nwm.extract_pdf_url(r.text) if r.status_code == 200 and on_host else None
                if pdf_url is None:
                    summary["errored"] += 1
                    _logger.warning("Asotin NTS advert %s: HTTP %d at %s, no notice PDF link",
                                    advert, r.status_code, r.url)
                    continue
                time.sleep(nwm.CRAWL_DELAY_S)
                text = _fetch_pdf_text(pdf_url)
                if len(text) < _NWM_MIN_TEXT_CHARS:
                    # A scanned or broken PDF is a failed read, never a "no sale" read.
                    summary["errored"] += 1
                    _logger.warning("Asotin NTS PDF %s yielded no usable text", pdf_url)
                    continue
                failed_before = summary["errored"] + summary.get("dropped_undated", 0)
                for block in nts_pdf.split_notice_blocks(text):
                    if not nwm.is_asotin_property(block):
                        summary["other_county"] += 1
                        continue
                    summary["upserted"] += _ingest_pdf_blocks(
                        db, NtsNotice, [block], pdf_url=pdf_url, source=nwm.SOURCE,
                        county=nwm.COUNTY, parse_fn=nts.parse_tacoma_notice, today=today,
                        summary=summary)
                db.commit()
                if summary["errored"] + summary.get("dropped_undated", 0) > failed_before:
                    # A notice failed to store, or a real sale whose date did not parse:
                    # not "read", so it is retried next run (and after a parser fix).
                    continue
                summary["read"] += 1
                if rds is not None:
                    rds.set(seen_key(advert, version), "1", ex=_NWM_SEEN_TTL_S)
            except Exception as exc:  # noqa: BLE001 — one advert must not stop the run
                db.rollback()
                summary["errored"] += 1
                _logger.warning("Asotin NTS advert %s failed: %s", advert, str(exc)[:120])

        # SOURCE-SCOPED expiry, as Clark: past auction or not re-read in _CACHE_DAYS.
        summary["expired"] = db.execute(
            _sa_text(
                """
                UPDATE nts_notices SET is_active = false
                WHERE source = :source AND is_active
                  AND (auction_date < :today OR fetched_at < :cutoff)
                """
            ),
            {"source": nwm.SOURCE, "today": today,
             "cutoff": datetime.now(UTC) - _td_days(_CACHE_DAYS)},
        ).rowcount or 0
        db.commit()
        # A read is the sitemap plus at least one parsed advert, or nothing new to read.
        if summary["read"] or (adverts and not todo):
            _record_crawl_heartbeat(db, nwm.COUNTY)

    _logger.info("Asotin NTS crawl done: %s", summary)
    # Most adverts are not Asotin trustee sales, so 0 upserts is normal (as Clark). Alert
    # when discovery found nothing, or when every advert this run attempted failed.
    _alert_if_crawl_barren(
        nwm.SOURCE, discovered=attempted or len(adverts), upserted=summary["upserted"],
        errored=attempted - summary["read"], alert_on_zero_upserts=False,
    )
    return summary


def _discover_latest_legals_pdf(page_url: str, pdf_path_prefix: str) -> str | None:
    """Find the newest legal-notices PDF URL on a Pacific Publishing paper's page.

    Returns the first href on the page that is (a) on the Pacific Publishing CDN host,
    (b) under the paper's expected legals path, and (c) whose filename contains "legal"
    (so the co-listed "CLASS …" classifieds PDF is excluded). The page lists issues
    newest-first, so the first match is the current week. None if nothing matches.
    """
    import html as _html
    import re
    from urllib.parse import urlparse

    from src.utils.safe_http import safe_get_following

    try:
        # The legal-notices page 3xx-redirects before serving content, so we follow
        # redirects (each hop re-validated against SSRF by safe_get_following).
        resp = safe_get_following(
            page_url, timeout=25, headers={"User-Agent": _PDF_BROWSER_UA}
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("NTS PDF discovery fetch failed (%s): %s", page_url, str(exc)[:140])
        return None
    # The CMS soft-404s these URLs (404 status, links present), so we do NOT gate on 200;
    # we require finding a valid legals PDF link instead. A hard error/empty body yields None.
    for raw in re.findall(r'href="([^"]+\.pdf)"', resp.text, re.I):
        url = _html.unescape(raw)
        parsed = urlparse(url)
        basename = parsed.path.rsplit("/", 1)[-1].lower()
        if (
            parsed.hostname == _PDF_HOST
            and parsed.path.startswith(pdf_path_prefix)
            and "legal" in basename
        ):
            return url
    _logger.info("NTS PDF discovery: no legals PDF link on %s (HTTP %d)", page_url, resp.status_code)
    return None


def _fetch_legals_blocks(pdf_url: str) -> list[str]:
    """Download one legals PDF (SSRF-guarded, byte-capped) and split it into notice
    blocks. Raises on download/extract failure — callers decide whether that is fatal.
    """
    return _fetch_legals_issue(pdf_url)[0]


def _fetch_legals_issue(pdf_url: str) -> tuple[list[str], bool]:
    """Like _fetch_legals_blocks, plus whether the text is a real legals issue.

    The flag separates "this week's issue carries no trustee sale" (a real issue,
    0 blocks) from "the extraction produced nothing usable" (0 blocks too).
    """
    from src.scrapers.sources import nts_pdf

    text = _fetch_pdf_text(pdf_url)
    return nts_pdf.split_notice_blocks(text), nts_pdf.looks_like_legals_issue(text)


def _fetch_pdf_text(pdf_url: str) -> str:
    """Download one PDF (SSRF-guarded, byte-capped) and return its normalized text.
    Raises on download/extract failure."""
    import os
    import tempfile

    from src.scrapers.sources import nts_pdf
    from src.utils.safe_http import safe_download_to_file

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        safe_download_to_file(
            pdf_url, path, max_bytes=_MAX_PDF_BYTES, require_https=True,
            headers={"User-Agent": _PDF_BROWSER_UA},
        )
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data))


def _ingest_pdf_blocks(
    db, model, blocks, *, pdf_url: str, source: str, county: str, parse_fn, today,
    summary: dict,
) -> int:
    """Parse + upsert every notice block from one PDF. Returns the number upserted.

    Each row gets its OWN savepoint so one bad notice cannot poison the PDF's batch.
    Caller commits.
    """
    from src.scrapers.sources import nts_tacoma_index as nts

    upserted = 0
    for block in blocks:
        try:
            parsed = parse_fn(block)
            row = nts.notice_to_row(
                parsed, source_url=pdf_url, today=today, source=source, county=county
            )
            if row is None:
                # Chrome vs. a real sale we failed to date — never the same counter.
                _note_undated_drop(summary, parsed, source)
                summary["skipped"] += 1
                continue
            row["fetched_at"] = datetime.now(UTC)
            with db.begin_nested():
                _upsert_notice(db, model, row, deed_reference=parsed.get("deed_reference"))
            upserted += 1
        except Exception as exc:  # noqa: BLE001
            summary["errored"] += 1
            _logger.warning("NTS PDF notice upsert failed (%s): %s", source, str(exc)[:140])
    return upserted


def _sweep_pdf_archive(
    db, model, *, source: str, county: str, parse_fn, today, summary: dict,
) -> None:
    """Re-read recent weekly issues we have not read lately, oldest first, on a budget.

    Oldest first is load-bearing: _upsert_notice refreshes every mutable field ON
    CONFLICT, and a notice republished across issues can legitimately change between
    them (an inline "SALE POSTPONED TO <later date>"), so the newest issue must write
    last. A 404 is the normal answer for a week with no issue (or a filename variant
    that missed) and is never fatal — this sweep is best-effort recovery layered UNDER
    the discover-current-issue pass, never a replacement for it.
    """
    from src.scrapers.sources.nts_pdf_archive import candidate_urls

    per_issue = candidate_urls(source, today, _ARCHIVE_WEEKS)   # oldest first
    flat = [u for group in per_issue for u in group]
    last_fetch = {
        r[0]: r[1] for r in db.execute(
            _sa_text(
                "SELECT source_url, max(fetched_at) FROM nts_notices "
                "WHERE source = :source AND source_url = ANY(:urls) GROUP BY source_url"
            ),
            {"source": source, "urls": flat},
        ).fetchall()
    }
    # "Has a row" is NOT the same as "fully ingested": the 2026-08-05 King issue had 2
    # of its 5 notices cached because the old splitter dropped the rest, and a
    # skip-if-any-row rule would have left those 3 lost forever. So an issue is due when
    # its newest row is older than _ARCHIVE_REFRESH_DAYS. Re-reading is idempotent (the
    # (source, ts_number) upsert refreshes in place), which also makes a future parser
    # fix self-healing — the same reasoning as _resweep_null_amount_notices.
    stale_before = datetime.now(UTC) - _td_days(_ARCHIVE_REFRESH_DAYS)

    def _due(group: list[str]) -> bool:
        seen = [last_fetch[u] for u in group if last_fetch.get(u) is not None]
        return not seen or max(seen) < stale_before

    due = [g for g in per_issue if _due(g)]      # still oldest first
    summary["archive_candidates"] = len(due)
    fetched = 0
    for group in due:
        if fetched >= _ARCHIVE_MAX_FETCH:
            break
        for url in group:            # filename variants, first hit wins
            try:
                blocks = _fetch_legals_blocks(url)
            except Exception as exc:  # noqa: BLE001
                # Overwhelmingly a 404: an unpublished week or a spelling that missed.
                # Info, not warning — alerting here would page on every quiet week.
                _logger.info("NTS archive sweep (%s): %s unavailable (%s)",
                             source, url.rsplit("/", 1)[-1], str(exc)[:100])
                time.sleep(_FETCH_DELAY_S)
                continue
            fetched += 1
            summary["archive_fetched"] += 1
            n = _ingest_pdf_blocks(
                db, model, blocks, pdf_url=url, source=source, county=county,
                parse_fn=parse_fn, today=today, summary=summary,
            )
            summary["archive_upserted"] += n
            # Commit per issue so a later failure cannot discard the weeks already
            # recovered in this run.
            db.commit()
            _logger.info("NTS archive sweep (%s): %s -> %d notices from %d blocks",
                         source, url.rsplit("/", 1)[-1], n, len(blocks))
            time.sleep(_FETCH_DELAY_S)
            break


def _crawl_pacific_publishing_pdf(
    *, page_url: str, pdf_path_prefix: str, source: str, county: str, parse_fn=None,
    archive: bool = False,
) -> dict:
    """Discover → download → extract → split → parse → upsert one weekly Legals PDF.

    Shared by every Pacific Publishing paper (Snohomish Tribune, Queen Anne News for
    King) — each task just passes its page/prefix/source/county. The PDF is streamed to
    a temp file with an SSRF guard + byte cap (safe_download_to_file), parsed by the
    shared nts_pdf adapter, and each notice block goes through the shared parser +
    notice_to_row(source, county) so the cache row carries the RIGHT county (the matcher
    scopes by county). Expiry is scoped to this source so one paper's crawl never expires
    another's rows.
    """
    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_tacoma_index as nts

    # Parser variance is isolated per paper (Codex): the King and Snohomish tasks
    # pass parse_king_notice / parse_snoho_notice for the surrogate-key layouts. The default
    # (shared colon parser) is kept for any caller that passes none.
    if parse_fn is None:
        parse_fn = nts.parse_nts_notice

    today = datetime.now(UTC).date()
    parsed_ok = False
    summary = {"source": source, "pdf_url": None, "blocks": 0,
               "upserted": 0, "skipped": 0, "errored": 0, "expired": 0,
               "archive_candidates": 0, "archive_fetched": 0, "archive_upserted": 0}

    pdf_url = _discover_latest_legals_pdf(page_url, pdf_path_prefix)
    summary["pdf_url"] = pdf_url

    blocks: list[str] = []
    current_upserted = 0
    issue_read = False
    if pdf_url:
        try:
            blocks, issue_read = _fetch_legals_issue(pdf_url)
        except Exception as exc:  # noqa: BLE001 — a bad download/PDF must not crash the beat
            summary["errored"] += 1
            _logger.warning("NTS PDF download/extract failed (%s): %s", pdf_url, str(exc)[:160])
    else:
        _logger.warning("NTS PDF crawl (%s): no current legals PDF found", source)
    summary["blocks"] = len(blocks)

    with system_sync_session() as db:
        if blocks:
            current_upserted = _ingest_pdf_blocks(
                db, NtsNotice, blocks, pdf_url=pdf_url, source=source, county=county,
                parse_fn=parse_fn, today=today, summary=summary,
            )
            summary["upserted"] += current_upserted
            db.commit()

        # Recovery pass. The current-issue fetch above was historically the ONLY writer
        # of this cache, so any week it missed was lost for good. Deliberately runs even
        # when that fetch just failed — a broken current issue is exactly the hole this
        # exists to fill. Best-effort: never let it fail the crawl.
        if archive:
            try:
                _sweep_pdf_archive(
                    db, NtsNotice, source=source, county=county,
                    parse_fn=parse_fn, today=today, summary=summary,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("NTS archive sweep failed (%s): %s", source, str(exc)[:160])
                db.rollback()
            summary["upserted"] += summary["archive_upserted"]

        summary["expired"] = db.execute(
            _sa_text(
                """
                UPDATE nts_notices SET is_active = false
                WHERE source = :source AND is_active
                  AND (auction_date < :today OR fetched_at < :cutoff)
                """
            ),
            {"source": source, "today": today,
             "cutoff": datetime.now(UTC) - _td_days(_CACHE_DAYS)},
        ).rowcount or 0
        db.commit()
        # Read AND parsed: a real issue whose trustee-sale notices all failed to parse
        # is parser drift, and must not look like a fresh cache with no sales.
        parsed_ok = not blocks or current_upserted > 0
        if issue_read and parsed_ok:
            _record_crawl_heartbeat(db, county)

    summary["issue_read"] = issue_read
    summary["heartbeat"] = issue_read and parsed_ok
    _logger.info("NTS PDF crawl done (%s): %s", source, summary)
    # Barren alerting deliberately judges the CURRENT ISSUE ONLY (Codex P2). Folding the
    # archive's recoveries into `upserted` would let a healthy back-catalogue hide the
    # exact failure this alert exists to catch: today's issue downloaded and split into
    # blocks, but every block failing to parse — i.e. the paper changed layout.
    _alert_if_crawl_barren(source, discovered=summary["blocks"], upserted=current_upserted)
    return summary


_NO_UPDATE = frozenset({"id", "source", "ts_number", "created_at"})

# Columns a re-crawl may only ever IMPROVE, never blank out.
#
# _upsert_notice refreshes every mutable field ON CONFLICT, so one pass that parses the
# same notice but fails to extract a field used to overwrite a good value with NULL.
# Observed in production 2026-09-04: Tacoma notice WA-26-1050840-BB held
# principal_owing = NULL while the lead matched from it still carried 575,150.38, and
# re-parsing its own source page with today's parser yields 575150.38 — the page never
# stopped saying "$575,150.38", we simply stored a NULL over the top of it. That is also
# why _resweep_null_amount_notices exists: it can add an amount back, but nothing stopped
# the next crawl from removing it again.
#
# A NULL from the parser means "this run could not read it", essentially never "the
# notice no longer states it" — trustees amend sale dates and amounts, they do not delete
# them. So a NULL is treated as no-information and the stored value stands.
# DELIBERATELY EXCLUDED: is_active (must be able to go false), fetched_at (must always
# advance), source_url and raw_hash (must track the issue actually read last).
_COALESCE_ON_UPDATE = frozenset({
    "auction_date", "auction_time", "auction_location", "parcel", "property_address",
    "property_address_normalized", "grantor", "trustee", "beneficiary",
    "principal_owing", "note_amount", "nod_date", "county", "state",
})


def _privacy_gate(db, row: dict) -> dict:
    """The notice to write under the privacy rules (Phase 6 P5e2, migration 132).

    The grantor is authorized as the trustee-sale lead it becomes (party on the
    notice's parcel). It is THIS crawl's grantor, or the stored one when the parser
    could not read it, so a crawl that misses the name cannot slip past a rule.
    WITHHELD or REVIEW: no grantor, inactive, the rule's id; the upsert keeps that
    state on every later crawl. One snapshot per notice, read in a SAVEPOINT;
    RuleSnapshotError propagates and nothing is written."""
    from src.privacy.authorize import ROW_COLUMNS, authorize, load_rule_snapshot

    with db.begin_nested():
        snapshot = load_rule_snapshot(db)
    row = {**row, "privacy_suppression_id": None}
    if not snapshot.by_anchor:
        return row
    stored = db.execute(
        _sa_text("SELECT grantor, parcel, county, state FROM nts_notices "
                 "WHERE source = :source AND ts_number = :ts"),
        {"source": row["source"], "ts": row["ts_number"]},
    ).first()

    def _pick(key: str):
        return row.get(key) or (getattr(stored, key) if stored is not None else None)

    grantor = _pick("grantor")
    if not grantor:
        return row
    lead = dict.fromkeys(ROW_COLUMNS) | {"parcel_id": _pick("parcel"), "party_name": grantor}
    decision = authorize(snapshot, lead, record_type="trustee_sale",
                         county=_pick("county"), state=_pick("state"))
    if decision.deliverable:
        return row
    return {**row, "grantor": None, "is_active": False,
            "privacy_suppression_id": decision.suppression_ids[0]}


def _upsert_notice(db, model, row: dict, *, deed_reference: str | None = None) -> None:
    """Upsert one notice on (source, ts_number); refresh all mutable fields.

    Idempotent: a re-crawl rewrites the mutable fields. id (PK) is set only on the
    INSERT path and is NEVER in the conflict-update set — refreshing an existing
    notice must not churn its PK or created_at. The natural key (source, ts_number)
    and id are stable across re-crawls.

    Value columns are COALESCEd (see _COALESCE_ON_UPDATE) so a re-crawl can improve a
    field but never blank one out — a NULL from the parser means "this run could not
    read it", not "the notice stopped saying it".

    ``deed_reference`` is the parsed deed-of-trust recording number. It is not a column;
    it only identifies this notice's REF-<recording #> surrogate twin (see below).
    """
    from uuid import uuid4

    from sqlalchemy import func as _func

    ts = row["ts_number"]
    was_inactive = False
    if ts.startswith("REF-"):
        prior = db.execute(
            _sa_text("SELECT is_active FROM nts_notices WHERE source = :source AND ts_number = :ts"),
            {"source": row["source"], "ts": ts},
        ).first()
        was_inactive = prior is not None and not prior[0]

    from src.scrapers.sources.nts_pdf_archive import issue_date_from_url

    # Derived here, not by each caller, so the beat crawl and the operator backfill
    # script cannot disagree (Codex). None for any non-PDF source URL.
    row = {**row, "id": str(uuid4())}
    row.setdefault("issue_date", issue_date_from_url(row.get("source_url")))
    row = _privacy_gate(db, row)
    stmt = pg_insert(model).values(**row)
    # A back issue read after a newer one must not put the newer one's values back
    # (a postponed sale reverting to its original date). Only PDF rows carry an
    # issue_date, so HTML sources keep newest-read-wins. An older issue still fills
    # gaps and still moves fetched_at/source_url, which the archive sweep's
    # "is this issue due" check reads (migration 127, Codex).
    older = and_(
        stmt.excluded.issue_date.isnot(None), model.issue_date.isnot(None),
        stmt.excluded.issue_date < model.issue_date,
    )

    def _value(c: str):
        new = (_func.coalesce(stmt.excluded[c], getattr(model, c))
               if c in _COALESCE_ON_UPDATE else stmt.excluded[c])
        if c in _COALESCE_ON_UPDATE or c == "is_active":
            return case((older, _func.coalesce(getattr(model, c), stmt.excluded[c])), else_=new)
        return new

    update_cols = {c: _value(c) for c in row if c not in _NO_UPDATE}
    if "principal_owing_basis" in update_cols:
        # The basis describes one amount, so it moves only with the amount it came
        # with: an older issue may supply it only when it also supplies the amount,
        # and a read without an amount keeps the stored pair (Codex).
        update_cols["principal_owing_basis"] = case(
            (older, case((model.principal_owing.isnot(None), model.principal_owing_basis),
                         else_=stmt.excluded.principal_owing_basis)),
            (stmt.excluded.principal_owing.isnot(None), stmt.excluded.principal_owing_basis),
            else_=model.principal_owing_basis,
        )
    update_cols["issue_date"] = _func.greatest(model.issue_date, stmt.excluded.issue_date)
    # A notice that carries a privacy rule stays withheld whatever this crawl read: no
    # grantor (not COALESCEd back), inactive (so no lead is built from it), the rule
    # kept. Lifting a rule is P7. Migration 132's CHECK enforces the same.
    held = _func.coalesce(model.privacy_suppression_id, stmt.excluded.privacy_suppression_id)
    update_cols["privacy_suppression_id"] = held
    update_cols["grantor"] = case((held.isnot(None), null()),
                                  else_=update_cols.get("grantor", model.grantor))
    update_cols["is_active"] = case((held.isnot(None), false()),
                                    else_=update_cols.get("is_active", model.is_active))
    stmt = stmt.on_conflict_do_update(
        constraint="uq_nts_notices_source_ts", set_=update_cols
    )
    db.execute(stmt)
    # Retire a trailing-dash twin: before 2026-09-02 the parser stored the trustee's
    # page-title spelling "WA-26-1035144-SW-" (dash and all); it now normalizes to
    # "WA-26-1035144-SW", which is a DIFFERENT natural key. Without this, both rows
    # stay active through the auction window and the same sale would surface twice
    # (Codex). Exact match on the dashed spelling only.
    db.execute(
        _sa_text(
            "UPDATE nts_notices SET is_active = false "
            "WHERE source = :source AND ts_number = :dashed AND is_active"
        ),
        {"source": row["source"], "dashed": row["ts_number"] + "-"},
    )
    # Retire a SURROGATE twin: a notice first ingested without a trustee sale number
    # is keyed REF-<deed recording #> (nts_king_pdf / parse_tacoma_notice). When a later
    # issue prints the real TS number for the same sale, that is a DIFFERENT natural
    # key, so without this both rows stay active and one sale surfaces as two auction
    # leads (Codex P1). The twin is identified EXACTLY by this notice's own recording
    # number: a recording number is unique per deed of trust, so equality is identity.
    # Parcel + auction date are deliberately NOT conditions (Codex P1 round 2): alone
    # they could retire a second lien's sale on the same parcel and day, and as an extra
    # filter they would miss a genuine twin whose sale was postponed (the date moves).
    # No all-digit deed reference -> retire nothing. Rows are never deleted.
    ref = (deed_reference or "").strip()
    if not ts.startswith(("REF-", "APN-")) and ref.isdigit():
        db.execute(
            _sa_text(
                "UPDATE nts_notices SET is_active = false "
                "WHERE source = :source AND ts_number = :twin AND is_active"
            ),
            {"source": row["source"], "twin": f"REF-{ref}"[:64]},
        )
    # Do not resurrect a retired surrogate. The upsert refreshes is_active, so
    # re-reading an OLDER issue that still prints the REF- version would switch the
    # retired twin back on next to its real row. nts_notices stores no deed reference,
    # so without a migration a real row's recording number cannot be compared. The
    # conservative rule: a REF- upsert never turns a row that WAS inactive back on while
    # an active non-surrogate row with the same source + parcel exists. The auction date
    # is deliberately NOT compared: a postponed sale moves the real row's date, and the
    # older REF- notice would otherwise come back to life beside it (Codex). An
    # already-active REF- row is left alone, and a REF- row with no such neighbour
    # activates normally. Worst case, a distinct surrogate on the same parcel that was
    # ALREADY inactive stays inactive; it can never switch an active row off.
    if was_inactive and row.get("parcel"):
        db.execute(
            _sa_text(
                "UPDATE nts_notices SET is_active = false "
                "WHERE source = :source AND ts_number = :ts AND is_active "
                "AND EXISTS (SELECT 1 FROM nts_notices r WHERE r.source = :source "
                "AND r.is_active AND r.ts_number NOT LIKE 'REF-%' "
                "AND r.ts_number NOT LIKE 'APN-%' "
                "AND r.parcel = :parcel)"
            ),
            {"source": row["source"], "ts": ts, "parcel": row["parcel"]},
        )


_RESWEEP_SELECT = _sa_text(
    """
    SELECT id, ts_number, source_url FROM nts_notices
    WHERE source = :source AND is_active AND auction_date >= :today
      AND privacy_suppression_id IS NULL  -- never re-read a withheld notice (132)
      -- An amount with no basis was parsed before migration 128: re-read it once so
      -- it stops showing as an amount of unknown type.
      AND (principal_owing IS NULL OR principal_owing_basis IS NULL)
      AND source_url IS NOT NULL
      AND (fetched_at IS NULL OR fetched_at < :stale_before)
    -- Missing amounts first: an amount of unknown basis only gets leftover capacity,
    -- so legacy rows that keep failing cannot crowd out amount recovery (Codex).
    ORDER BY (principal_owing IS NOT NULL), auction_date ASC
    LIMIT :lim
    """
)
_TOUCH_FETCHED = _sa_text("UPDATE nts_notices SET fetched_at = :now WHERE id = :id")


def _resweep_null_amount_notices(
    db, model, *, source: str, county: str, today, fetch, parse, notice_to_row,
) -> dict:
    """Re-fetch a bounded batch of active, future-dated notices with NO amount (or an
    amount of unknown basis, parsed before migration 128).

    ``fetch(url) -> (status_code, html)``; ``parse(html) -> parsed dict``;
    ``notice_to_row`` is the source's row builder. A re-parse that now yields an
    amount is upserted (updated); one that still has none only refreshes fetched_at
    (unchanged_null) so it is retried at most once per _RESWEEP_MIN_AGE_HOURS; a
    non-200 page also just refreshes fetched_at and is counted (not_found) — the
    notice is NEVER deactivated from one bad fetch, auction-date expiry handles a
    page the source really removed (Codex). Errors never abort the crawl.
    """
    counts = {"attempted": 0, "updated": 0, "unchanged_null": 0, "not_found": 0, "errors": 0}
    stale_before = datetime.now(UTC) - timedelta(hours=_RESWEEP_MIN_AGE_HOURS)
    rows = db.execute(
        _RESWEEP_SELECT,
        {"source": source, "today": today, "stale_before": stale_before, "lim": _RESWEEP_LIMIT},
    ).fetchall()
    for r in rows:
        counts["attempted"] += 1
        now = datetime.now(UTC)
        try:
            status, html = fetch(r.source_url)
            if status != 200:
                counts["not_found"] += 1
                db.execute(_TOUCH_FETCHED, {"now": now, "id": r.id})
                continue
            parsed = parse(html)
            row = notice_to_row(parsed, source_url=r.source_url, today=today,
                                source=source, county=county)
            if row is None or row.get("principal_owing") is None:
                counts["unchanged_null"] += 1
                db.execute(_TOUCH_FETCHED, {"now": now, "id": r.id})
                continue
            row["fetched_at"] = now
            _upsert_notice(db, model, row, deed_reference=parsed.get("deed_reference"))
            counts["updated"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad notice must not stop the sweep
            counts["errors"] += 1
            _logger.warning("NTS resweep %s failed: %s", r.source_url, str(exc)[:120])
        time.sleep(_FETCH_DELAY_S)
    if counts["attempted"]:
        _logger.info("NTS resweep (%s): %s", source, counts)
    return counts


def _barren_alert_reason(
    discovered: int, upserted: int, alert_on_zero_upserts: bool = True, errored: int = 0
) -> str | None:
    """Pure decision: the reason string to alert on a barren crawl, or None if healthy.

    discovered == 0 -> discovery broke; discovered > 0 but upserted == 0 -> parse broke;
    any upsert -> healthy (no alert).

    ``alert_on_zero_upserts=False`` (Clark/Columbian) suppresses the "0 upserted" branch:
    that crawler counts EVERY legal-notice ad as "discovered" (not just trustee sales),
    so 0 upserts is the NORMAL no-sale-today case, not a parser break. BUT if every ad
    fetch failed (``errored >= discovered``) then 0 upserts IS a real failure — the source
    is down/blocked, not sale-free — so we still alert (Codex P2).
    """
    if discovered == 0:
        return "0 notices discovered. Listing/PDF discovery may have broken"
    if alert_on_zero_upserts and upserted == 0:
        return f"{discovered} notices discovered but 0 upserted. The parser may have broken"
    if not alert_on_zero_upserts and upserted == 0 and errored >= discovered:
        return (
            f"{discovered} ads discovered but all {errored} detail fetches failed. "
            "The source may be down or blocking"
        )
    return None


def _note_undated_drop(summary: dict, parsed: dict, source: str) -> bool:
    """Record + log a discarded block that still carries a trustee-sale identity.

    ``notice_to_row`` returns None whenever ``is_valid_nts`` is False (it needs BOTH a
    ts_number and an auction_date). Most of those really are not NTS bodies — summons,
    probate, RFPs, bids — and must stay quiet, so every drop shared one silent
    ``skipped`` counter.

    That hid a real outage: a block WITH a ts_number but WITHOUT an auction date is not
    chrome, it is a published trustee sale whose date shape the parser could not read —
    a lost lead. King/Affinia numeric auction dates went undetected this way until
    2026-09-03, by which point the current issue was dropping 100% of its notices.
    Splitting that case out is what makes the next parser gap visible on day one.

    Returns True when the drop was an undated real notice (caller counts it as such).
    """
    from src.scrapers.sources.nts_tacoma_index import _to_date

    # Convertibility, not mere presence (Codex): a captured-but-unparseable date
    # ("13/40/2026", OCR garbage) also fails is_valid_nts downstream and is the SAME
    # class of parser failure — it must not slip back into the quiet `skipped` bucket.
    if not parsed.get("ts_number") or _to_date(parsed.get("auction_date")):
        return False
    summary["dropped_undated"] = summary.get("dropped_undated", 0) + 1
    _logger.warning(
        "NTS drop (%s): notice %s has no parseable auction date — parser gap, lead lost",
        source, str(parsed.get("ts_number"))[:64],
    )
    return True


def _alert_if_crawl_barren(
    source: str, *, discovered: int, upserted: int, alert_on_zero_upserts: bool = True,
    errored: int = 0,
) -> None:
    """OPS alert when a crawl run produced no usable notices.

    This is the silent-failure mode that let the Pierce NTS cache go stale for a week
    unnoticed (the crawler bailed at page 1 on days page 1 had no trustee sale). Two
    barren conditions warrant a page:
      * discovered == 0 — listing/PDF discovery broke (layout change, block, or the
        old page-1 break bug), so nothing was even fetched to parse.
      * discovered > 0 but upserted == 0 — everything failed to parse/validate (a
        parser/format drift), so no auction data reaches leads.
    No-op unless OPS_ALERT_EMAIL is configured; send_ops_alert carries its own cooldown
    and never raises. A healthy run (upserted > 0) sends nothing.
    """
    reason = _barren_alert_reason(discovered, upserted, alert_on_zero_upserts, errored)
    if reason is None:
        return
    from src.workers.ops_alerts import send_ops_alert

    send_ops_alert(
        kind="nts_crawl_barren",
        key=f"nts_crawl_barren:{source}",
        subject=f"NTS crawler produced nothing ({source})",
        body=(
            f"The NTS crawler '{source}' {reason}.\n\n"
            "auction_date / default_amount enrichment for pre_foreclosure leads in this "
            "county will go stale until this is fixed. Check the source site layout and "
            "the crawler logs."
        ),
    )


def _record_crawl_heartbeat(db, county: str) -> None:
    """Record that this county's crawler READ its source (trustee_sale freshness).

    Only ever marks healthy: the source canary probes throttled/blocked rows, so a
    heartbeat row is never probed. Best-effort, like every source_health write.
    """
    try:
        from src.scrapers.enrichment.source_health import mark_source_healthy
        from src.scrapers.trustee_sale import nts_crawl_heartbeat_key

        mark_source_healthy(db, nts_crawl_heartbeat_key(county))
    except Exception as exc:  # noqa: BLE001 — the crawl's own data is already committed
        _logger.error("NTS crawl heartbeat not recorded for %s: %s", county, str(exc)[:160])


# Benton's recorded notices are read straight off the recorder (no newspaper source):
# a notice is recorded 90-120 days before its sale (RCW 61.24.040), so the window
# covers every sale still ahead plus the discontinuances that cancel them.
_BENTON_RECORDER_WINDOW_DAYS = 150


@app.task(name="src.workers.nts_crawler.crawl_nts_benton_recorder")
def crawl_nts_benton_recorder() -> dict:
    """Crawl Benton County's recorded Notices of Trustee's Sale into nts_notices.

    Every NTS recorded in the window is read off its recorded image; a notice with a
    sale date and exactly one Benton parcel is upserted (key REC-<instrument>), and
    one a recorded Discontinuance names is stored inactive. Multi-property and
    undated notices are counted, never stored as auction leads.
    """
    import asyncio

    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_benton_crawl as crawl
    from src.scrapers.sources import nts_benton_recorder as rec

    today = datetime.now(UTC).date()
    start = today - timedelta(days=_BENTON_RECORDER_WINDOW_DAYS)
    summary = {"source": rec.SOURCE, "discovered": 0, "upserted": 0, "skipped": 0,
               "errored": 0, "cancelled": 0}
    try:
        got = asyncio.run(crawl.collect(start.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y")))
    except Exception as exc:  # noqa: BLE001 — an unreadable search must page, not crash the beat
        _logger.warning("Benton recorder NTS crawl failed: %s", str(exc)[:200])
        _alert_if_crawl_barren(rec.SOURCE, discovered=0, upserted=0)
        return summary
    summary["discovered"] = got.nts_rows
    summary["errored"] = got.failed
    cancelled = frozenset(got.cancelled)
    with system_sync_session() as db:
        for parsed, url in got.notices:
            row = rec.recorded_notice_to_row(parsed, url, today, cancelled)
            if row is None:
                summary["skipped"] += 1
                continue
            row["fetched_at"] = datetime.now(UTC)  # the past-sale matcher pass reads it
            _upsert_notice(db, NtsNotice, row)
            summary["upserted"] += 1
        if cancelled:
            # Applied to every stored notice the discontinuances name, whether or not
            # its own image was read today; cancelled_at is never cleared (Codex P1).
            summary["cancelled"] = _cancel_notices(db, rec.SOURCE, [rec.ts_key(i) for i in sorted(cancelled)])
        db.commit()
        # Fresh only when the source was actually read: some notice parsed, or the
        # window conclusively held none (Codex P2).
        if got.nts_rows == 0 or got.failed < got.nts_rows:
            _record_crawl_heartbeat(db, rec.COUNTY)
    _alert_if_crawl_barren(rec.SOURCE, discovered=summary["discovered"], upserted=summary["upserted"],
                           errored=summary["errored"])
    _logger.info("Benton recorder NTS crawl: %s", summary)
    return summary


def _cancel_notices(db, source: str, keys: list[str]) -> int:
    """The sales these notices announced are off (a recorded discontinuance, or a
    later notice that replaces them): cancelled_at set once and never cleared, inactive,
    and leads already stamped with them cleared so the matcher can attach a live notice
    (Codex P1). Exclusive against the matcher/finalizer's shared NTS_CANCEL_LOCK_KEY
    (re-entrant within one transaction). Returns the notices changed. Caller commits."""
    from src.workers.nts_matcher_task import NTS_CANCEL_LOCK_KEY

    db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"), {"k": NTS_CANCEL_LOCK_KEY})
    res = db.execute(
        _sa_text("UPDATE nts_notices SET cancelled_at = COALESCE(cancelled_at, now()), is_active = false "
                 "WHERE source = :source AND ts_number = ANY(:keys)"),
        {"source": source, "keys": keys},
    )
    # A later statement, so its snapshot sees an attachment that committed while we waited.
    db.execute(
        _sa_text("""
            UPDATE results SET auction_date = NULL, default_amount = NULL,
                nts_match_confidence = NULL, nts_notice_id = NULL,
                enrichment_data = (CASE WHEN jsonb_typeof(enrichment_data::jsonb) = 'object'
                                        THEN enrichment_data::jsonb - 'nts'
                                        ELSE enrichment_data::jsonb END)
            WHERE nts_notice_id IN (
                SELECT id FROM nts_notices WHERE source = :source AND ts_number = ANY(:keys))
        """),
        {"source": source, "keys": keys},
    )
    return res.rowcount or 0


def _td_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)


# ── Chelan County recorder (AcclaimWeb): NTS documents OCR'd into staging (4a) ──
# Chelan publishes its trustee sales only as recorded scans (no text layer, no
# newspaper source found). Each new NTS recording is OCR'd into nts_recorder_documents
# for an operator to compare with its image; nothing here reaches nts_notices or a
# customer (phase 4a-3 does that on approval). Detail pages and PDFs need no session
# (live 2026-11-12), so only the search uses a browser.
_CHELAN_RECORDER = "https://acclaim.co.chelan.wa.us/AcclaimWeb"
_CHELAN_NTS_CODES = {"NTS", "NTSCOM"}
_CHELAN_WINDOW_DAYS = 30        # recordings are indexed late; overlapping windows heal it
_CHELAN_MAX_DOCS = 20           # per run (~2 NTS are recorded a month)
_CHELAN_DEADLINE_S = 40 * 60    # under the task's soft limit
_CHELAN_PACE_S = 3.0
_CHELAN_LOCK_KEY = "bl:nts_chelan_recorder:lock"
_RECORDER_PDF_MAX_BYTES = 20 * 1024 * 1024
# A pending row older than this loses its OCR text (it names the borrower).
RECORDER_PENDING_TTL_DAYS = 30
RECORDER_EXPIRED_NOTE = "expired unreviewed"
# A notice waiting longer than this for review is a lead customers are not getting, so
# the county's cache stops counting as fresh (the trustee_sale scraper reports it).
RECORDER_REVIEW_LAG_DAYS = 3


def chelan_cache_is_fresh(summary: dict) -> bool:
    """Whether a crawl run may record Chelan's NTS heartbeat: the source was read in
    full (discovery done, every recording staged or already settled). Pure. Review lag
    is judged separately, at delivery time (chelan_unresolved_reason)."""
    if summary.get("error") or summary.get("skipped"):
        return False
    return not any(summary.get(k) for k in ("failed", "invalid", "unread"))


def chelan_unresolved_reason(db, now: datetime) -> str | None:
    """Why Chelan's notice cache is short, or None: a recording first seen more than
    RECORDER_REVIEW_LAG_DAYS ago that is still awaiting review, or could not be read
    (failed, including expired unreviewed), until an operator approves or rejects it.
    It counts wherever the recording now sits, inside the crawl window or not (Codex)."""
    from sqlalchemy import func, select

    from src.db.models import NtsRecorderDocument as Doc

    oldest = db.execute(select(func.min(Doc.created_at)).where(
        Doc.county == "chelan", Doc.status.in_(("pending_review", "failed")),
        Doc.created_at < now - timedelta(days=RECORDER_REVIEW_LAG_DAYS))).scalar()
    if oldest is None:
        return None
    return (f"recorded trustee-sale notices unresolved since {oldest.date().isoformat()} "
            "(operator review: scripts/nts_review.py list)")


def purge_stale_recorder_documents(db, now: datetime | None = None) -> int:
    """Pending-review rows older than RECORDER_PENDING_TTL_DAYS: drop the OCR text and
    mark them failed ("expired unreviewed"), so a notice nobody reviewed never keeps a
    borrower's name. Returns the rows changed. Caller commits."""
    from sqlalchemy import func, update

    from src.db.models import NtsRecorderDocument as Doc

    now = now or datetime.now(UTC)
    res = db.execute(
        update(Doc)
        .where(Doc.status == "pending_review",
               Doc.created_at < now - timedelta(days=RECORDER_PENDING_TTL_DAYS))
        .values(status="failed", ocr_text=None, decision_note=RECORDER_EXPIRED_NOTE, updated_at=now,
                # OCR'd free text (an address, a trustee line) could carry a name too: an
                # expired row keeps only the recording's own facts.
                fields=func.jsonb_build_object(
                    "recording_apns", Doc.fields["recording_apns"],
                    "related_deeds_of_trust", Doc.fields["related_deeds_of_trust"]))
    )
    return res.rowcount or 0


def stage_recorder_document(db, *, county: str, state: str, doc: dict, detail_html: str | None,
                            pdf: bytes | None, tesseract: str | None = None) -> str:
    """OCR one discovered recording into staging; returns the status written, or
    "kept" when an existing row must not change. Caller commits.

    A new row, or a FAILED one (not expired), is (re)written. A row awaiting review,
    approved, rejected or superseded is never touched: a re-read must not replace what
    an operator has seen or decided.
    """
    from uuid import uuid4

    from src.db.models import NtsRecorderDocument as Doc
    from src.scrapers.sources.nts_recorder_ocr import (
        PARSER_VERSION,
        corroborate,
        read_notice,
        related_deeds_of_trust,
    )
    from src.scrapers.templates.acclaimweb import parse_detail_apns

    now = datetime.now(UTC)
    record_date = datetime.strptime(doc["record_date"], "%m/%d/%Y").date()
    apns = parse_detail_apns(detail_html) if detail_html else None
    dts = related_deeds_of_trust(detail_html) if detail_html else None
    kw = {"tesseract": tesseract} if tesseract else {}
    out = (read_notice(pdf, record_date, **kw) if pdf
           else {"ok": False, "reason": "pdf not downloaded", "pdf_sha256": None})
    values = {
        "county": county, "state": state, "transaction_item_id": doc["item_id"],
        "instrument_number": doc["instrument"], "doc_code": doc["doc_code"],
        "record_date": record_date,
        "recording_apn": apns[0] if apns and len(apns) == 1 else None,
        "related_deed_of_trust": dts[0] if dts and len(dts) == 1 else None,
        "pdf_sha256": out.get("pdf_sha256"), "parser_version": PARSER_VERSION,
        "ocr_engine": "tesseract", "updated_at": now,
    }
    if out["ok"]:
        agree, cflags = corroborate(out["fields"], apns, dts)
        values.update(
            status="pending_review", ocr_text=out["text"], page_count=out["pages"],
            decision_note=None,
            fields={"fields": out["fields"], "evidence": out["evidence"],
                    "flags": out["flags"] + cflags, "corroborated": agree,
                    "recording_apns": apns, "related_deeds_of_trust": dts},
        )
    else:
        values.update(status="failed", ocr_text=None, page_count=None,
                      decision_note=str(out["reason"])[:500],
                      fields={"recording_apns": apns, "related_deeds_of_trust": dts})

    stmt = pg_insert(Doc).values(id=str(uuid4()), **values)
    retryable = and_(Doc.status == "failed",
                     Doc.decision_note.is_distinct_from(RECORDER_EXPIRED_NOTE))
    stmt = stmt.on_conflict_do_update(
        constraint="uq_nts_recorder_documents_county_item",
        set_={k: stmt.excluded[k] for k in values if k not in ("county", "transaction_item_id")},
        where=retryable,
    ).returning(Doc.status)
    row = db.execute(stmt).first()
    return row[0] if row else "kept"


@app.task(name="src.workers.nts_crawler.purge_nts_recorder_staging")
def purge_nts_recorder_staging() -> int:
    """Daily, on its own schedule (not only when a crawl runs): unreviewed staged
    notices past RECORDER_PENDING_TTL_DAYS lose their OCR text and OCR'd fields."""
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        n = purge_stale_recorder_documents(db)
        db.commit()
    if n:
        _logger.info("NTS recorder staging: %d unreviewed notices expired", n)
    return n


# ── operator review: a staged recording becomes a notice only on approval (4a-3a) ──
CHELAN_RECORDER_SOURCE = "chelan_recorder"
_AMOUNT_BASES = ("principal", "sum_owing")   # migration 128's CHECK
_AMOUNT_MAX = 10 ** 10                       # nts_notices.principal_owing is Numeric(12, 2)
# Fixed codes, not free text: a typed reason could carry the borrower's name past the
# point where the OCR text is dropped (Codex).
REJECT_REASONS = ("not_a_trustee_sale_notice", "unreadable", "sale_not_confirmable",
                  "wrong_property", "duplicate_recording")


class ReviewError(ValueError):
    """An approval or rejection the staged row's state or the reviewer's input forbids."""


def _recorder_doc_for_review(db, doc_id: str, statuses: tuple[str, ...] = ("pending_review",)):
    from sqlalchemy import select

    from src.db.models import NtsRecorderDocument as Doc

    doc = db.execute(select(Doc).where(Doc.id == doc_id).with_for_update()).scalar_one_or_none()
    if doc is None:
        raise ReviewError("no staged document with that id")
    if doc.status not in statuses:
        raise ReviewError(f"the document is {doc.status}, not " + " or ".join(s.replace("_", " ") for s in statuses))
    if (doc.county, doc.state) != ("chelan", "WA"):
        raise ReviewError("only Chelan County recordings are reviewed here")
    return doc


def _recording_facts(doc) -> dict:
    f = doc.fields or {}
    return {"recording_apns": f.get("recording_apns"), "related_deeds_of_trust": f.get("related_deeds_of_trust")}


def approve_recorder_document(db, doc_id: str, *, reviewer: str, values: dict,
                              supersedes: str | None = None, independent: bool = False,
                              today=None) -> str:
    """The operator has compared the staged read with the recorded image: write the
    notice they confirmed. Returns the nts_notices id. Caller commits.

    ``values`` are what the reviewer read off the image: auction_date (date), parcel
    and grantor are required; auction_time, property_address, principal_owing (Decimal)
    with principal_owing_basis, trustee and beneficiary are optional. The parcel must be
    one of the recording's own APNs. The grantor goes through the privacy gate into
    nts_notices only, never into staging.

    A later notice for the same sale is linked EXPLICITLY (``supersedes``: the older
    approved recording's instrument number), never inferred: one deed of trust can carry
    successive foreclosures (Codex P1). While another approved, live notice shares this
    recording's deed of trust, approval refuses until the reviewer says which it is.
    The whole decision holds NTS_CANCEL_LOCK_KEY, so two approvals cannot both win.
    """
    import hashlib
    from decimal import Decimal

    from sqlalchemy import select

    from src.db.models import NtsNotice
    from src.db.models import NtsRecorderDocument as Doc
    from src.utils.address_intel import address_match_key
    from src.workers.nts_matcher_task import NTS_CANCEL_LOCK_KEY

    if supersedes and independent:
        raise ReviewError("--supersedes and --independent contradict each other")
    db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"), {"k": NTS_CANCEL_LOCK_KEY})
    doc = _recorder_doc_for_review(db, doc_id)
    today = today or datetime.now(UTC).date()

    parcel = (values.get("parcel") or "").strip()
    if parcel not in (_recording_facts(doc)["recording_apns"] or []):
        raise ReviewError("the parcel is not one of this recording's own APNs")
    auction = values.get("auction_date")
    if auction is None:
        raise ReviewError("a sale date is required")
    grantor = " ".join((values.get("grantor") or "").split())
    if not grantor:
        raise ReviewError("the grantor (borrower) read off the image is required")
    amount, basis = values.get("principal_owing"), values.get("principal_owing_basis")
    if (amount is None) != (basis is None):
        raise ReviewError("an amount needs its basis and a basis needs its amount")
    if amount is not None and not (
            basis in _AMOUNT_BASES and isinstance(amount, Decimal) and amount.is_finite()
            and 0 < amount < _AMOUNT_MAX and amount == amount.quantize(Decimal("0.01"))):
        raise ReviewError(f"the amount must be a positive dollar amount (cents at most) "
                          f"and its basis one of {_AMOUNT_BASES}")

    def _key(instrument: str) -> str:
        return f"REC-{instrument}"

    live_approved = (
        select(Doc).join(NtsNotice, NtsNotice.id == Doc.nts_notice_id)
        .where(Doc.county == doc.county, Doc.state == doc.state, Doc.status == "approved",
               NtsNotice.cancelled_at.is_(None))
    )
    target = None
    if supersedes:
        target = db.execute(live_approved.where(Doc.instrument_number == supersedes)
                            .with_for_update(of=Doc)).scalar_one_or_none()
        if target is None:
            raise ReviewError("--supersedes names no approved, live notice")
        def _order(d):  # recorded later = newer; same day: the higher instrument number
            return (d.record_date, int(d.instrument_number) if d.instrument_number.isdigit() else -1)

        if _order(target) >= _order(doc):
            raise ReviewError("a notice can only supersede an earlier recording")
    elif not independent:
        # Every deed of trust the recording cites, not only a single one (Codex High).
        dts = set(_recording_facts(doc)["related_deeds_of_trust"] or [])
        same_dt = [d for d in db.execute(live_approved).scalars()
                   if dts & set(_recording_facts(d)["related_deeds_of_trust"] or [])]
        if same_dt:
            raise ReviewError(
                "approved notice(s) " + ", ".join(sorted(d.instrument_number for d in same_dt))
                + " share this deed of trust: pass --supersedes <instrument> or --independent")

    key = _key(doc.instrument_number)
    already_cancelled = db.execute(
        select(NtsNotice.cancelled_at).where(NtsNotice.source == CHELAN_RECORDER_SOURCE,
                                             NtsNotice.ts_number == key)).scalar()
    addr = " ".join((values.get("property_address") or "").split())[:512] or None
    approved = {
        "auction_date": auction.isoformat(), "parcel": parcel,
        "auction_time": (values.get("auction_time") or None),
        "property_address": addr,
        "principal_owing": None if amount is None else str(amount),
        "principal_owing_basis": basis,
        "trustee": (values.get("trustee") or None), "beneficiary": (values.get("beneficiary") or None),
    }
    payload = "|".join(str(v or "") for v in (doc.instrument_number, *approved.values()))
    now = datetime.now(UTC)
    _upsert_notice(db, NtsNotice, {
        "source": CHELAN_RECORDER_SOURCE, "ts_number": key, "county": "chelan", "state": "WA",
        "parcel": parcel, "property_address": addr,
        "property_address_normalized": (address_match_key(addr) or None) if addr else None,
        "auction_date": auction, "auction_time": (approved["auction_time"] or "")[:16] or None,
        "auction_location": None, "grantor": grantor[:512],
        "trustee": (approved["trustee"] or "")[:255] or None,
        "beneficiary": (approved["beneficiary"] or "")[:255] or None,
        "principal_owing": amount, "principal_owing_basis": basis,
        "note_amount": None, "nod_date": None,
        "source_url": f"{_CHELAN_RECORDER}/details/documentdetails/{doc.transaction_item_id}/1/1/11",
        "raw_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "is_active": auction >= today and already_cancelled is None,
        "fetched_at": now,
    })
    notice_id = db.execute(select(NtsNotice.id).where(NtsNotice.source == CHELAN_RECORDER_SOURCE,
                                                      NtsNotice.ts_number == key)).scalar_one()
    if target is not None:
        _cancel_notices(db, CHELAN_RECORDER_SOURCE, [_key(target.instrument_number)])
        target.status = "superseded"
        target.decision_note = f"superseded by {doc.instrument_number}"
        target.updated_at = now
    doc.status, doc.ocr_text, doc.nts_notice_id = "approved", None, notice_id
    doc.reviewed_by, doc.reviewed_at, doc.updated_at = reviewer, now, now
    doc.decision_note = (f"supersedes {supersedes}" if supersedes
                         else "independent of other notices on its deed of trust" if independent else None)
    doc.fields = {**_recording_facts(doc), "approved": approved}  # no grantor, no OCR text
    return str(notice_id)


def reject_recorder_document(db, doc_id: str, *, reviewer: str, reason: str) -> None:
    """The staged read is not a usable notice (wrong document, unreadable, a sale the
    reviewer cannot confirm): nothing reaches nts_notices, and the OCR text and OCR'd
    fields go. ``reason`` is one of REJECT_REASONS. A recording that could not be read
    (failed) can be rejected too, so a scan no retry will ever read stops being retried
    and stops holding the county's cache stale. Caller commits."""
    if reason not in REJECT_REASONS:
        raise ReviewError(f"the reason must be one of {REJECT_REASONS}")
    doc = _recorder_doc_for_review(db, doc_id, ("pending_review", "failed"))
    now = datetime.now(UTC)
    doc.status, doc.ocr_text, doc.decision_note = "rejected", None, reason
    doc.reviewed_by, doc.reviewed_at, doc.updated_at = reviewer, now, now
    doc.fields = _recording_facts(doc)


# Atomic "delete the lock only if it is still ours" (a plain GET then DELETE could
# delete a successor's lock if ours expired in between).
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)
_ITEM_ID_RE = re.compile(r"[0-9]{1,32}")
# Time one document can take: detail + PDF fetch, pacing, and OCR's own wall limit.
_DETAIL_FETCH_S = 30           # whole detail-page download (wall clock)
_PDF_FETCH_S = 120             # whole PDF download (wall clock)
_DETAIL_MAX_BYTES = 1024 * 1024
# Time one document can take: both downloads (each may overrun by one 60 s read
# timeout), pacing, OCR's own wall limit, and a margin for the database write.
_CHELAN_DOC_RESERVE_S = _DETAIL_FETCH_S + _PDF_FETCH_S + 2 * 60 + 2 * _CHELAN_PACE_S + 300 + 60


@app.task(name="src.workers.nts_crawler.crawl_nts_chelan_recorder",
          soft_time_limit=50 * 60, time_limit=55 * 60)
def crawl_nts_chelan_recorder() -> dict:
    """Stage Chelan County NTS recordings for review (beat: crawl-nts-chelan-recorder).

    Purge first, then: recorder search (browser) for NTS/NTSCOM over the last
    _CHELAN_WINDOW_DAYS -> each recording not yet staged -> its detail page and PDF
    (SSRF-guarded, byte-capped, no browser) -> OCR -> nts_recorder_documents. One run at
    a time (Redis lock, released atomically); one time budget from the moment the lock
    is taken, covering discovery and every document; every recording ends up counted as
    staged, failed, kept, invalid or unread, and anything not staged raises an alert.
    """
    import asyncio
    from uuid import uuid4

    import redis as sync_redis
    from celery.exceptions import SoftTimeLimitExceeded
    from sqlalchemy import select

    from src.config import settings
    from src.db.models import NtsRecorderDocument as Doc
    from src.db.session import system_sync_session
    from src.scrapers.templates.acclaimweb import ChelanAcclaimWebScraper

    summary = {"source": "chelan_recorder", "expired": 0, "discovered": 0, "staged": 0,
               "failed": 0, "kept": 0, "invalid": 0, "unread": 0}
    rds = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    token = str(uuid4())
    if not rds.set(_CHELAN_LOCK_KEY, token, nx=True, ex=_CHELAN_DEADLINE_S + 20 * 60):
        _logger.info("Chelan NTS recorder crawl already running; skipped")
        rds.close()
        return {**summary, "skipped": "locked"}
    deadline = time.monotonic() + _CHELAN_DEADLINE_S
    error: str | None = None
    try:
        with system_sync_session() as db:
            summary["expired"] = purge_stale_recorder_documents(db)
            db.commit()
            retry = and_(Doc.status == "failed",
                         Doc.decision_note.is_distinct_from(RECORDER_EXPIRED_NOTE))
            settled = {r[0] for r in db.execute(
                select(Doc.transaction_item_id).where(Doc.county == "chelan", ~retry))}

        today = datetime.now(UTC).date()
        start = (today - timedelta(days=_CHELAN_WINDOW_DAYS)).strftime("%m/%d/%Y")

        async def _discover() -> list[dict]:
            s = ChelanAcclaimWebScraper(_CHELAN_RECORDER, "chelan", "wa",
                                        record_types=["pre_foreclosure"],
                                        record_type="pre_foreclosure")
            async with s:
                return await s.discover(start, today.strftime("%m/%d/%Y"), _CHELAN_NTS_CODES)

        async def _bounded() -> list[dict]:
            budget = deadline - time.monotonic() - _CHELAN_DOC_RESERVE_S
            return await asyncio.wait_for(_discover(), timeout=max(budget, 1))

        try:
            docs = asyncio.run(_bounded())
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 -- reported, not hidden: no partial list is used
            error = f"discovery failed: {type(exc).__name__}: {str(exc)[:200]}"
            return {**summary, "error": "discovery failed"}
        summary["discovered"] = len(docs)
        valid = [d for d in docs if _ITEM_ID_RE.fullmatch(d.get("item_id") or "")]
        summary["invalid"] = len(docs) - len(valid)
        todo = [d for d in valid if d["item_id"] not in settled]
        summary["kept"] = len(valid) - len(todo)  # already staged or decided
        headers = {"User-Agent": _PDF_BROWSER_UA}
        for i, doc in enumerate(todo):
            left = deadline - time.monotonic()
            if i >= _CHELAN_MAX_DOCS or left < _CHELAN_DOC_RESERVE_S:
                summary["unread"] = len(todo) - i
                break
            time.sleep(_CHELAN_PACE_S)
            detail_html = pdf = None
            try:
                detail_html = _bounded_fetch(
                    f"{_CHELAN_RECORDER}/details/documentdetails/{doc['item_id']}/1/1/11",
                    _DETAIL_MAX_BYTES, _DETAIL_FETCH_S, headers).decode("utf-8", "replace")
                time.sleep(_CHELAN_PACE_S)
                pdf = _bounded_fetch(
                    f"{_CHELAN_RECORDER}/Image/DocumentPdfAllPages/{doc['item_id']}",
                    _RECORDER_PDF_MAX_BYTES, _PDF_FETCH_S, headers)
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 -- staged as failed below, with the reason
                _logger.warning("Chelan NTS %s fetch failed: %s", doc["item_id"], type(exc).__name__)
            try:
                with system_sync_session() as db:
                    try:
                        status = stage_recorder_document(db, county="chelan", state="WA", doc=doc,
                                                         detail_html=detail_html, pdf=pdf)
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 -- counted and alerted, the run goes on
                _logger.warning("Chelan NTS %s not staged: %s", doc["item_id"], type(exc).__name__)
                status = "failed"
            summary["staged" if status == "pending_review" else status] += 1
        with system_sync_session() as db:
            if chelan_cache_is_fresh(summary):
                _record_crawl_heartbeat(db, "chelan")
            error = chelan_unresolved_reason(db, datetime.now(UTC))
        _logger.info("Chelan NTS recorder crawl: %s", summary)
        return summary
    except SoftTimeLimitExceeded:
        error = "time limit reached mid-run"
        raise
    except Exception as exc:
        error = f"run failed: {type(exc).__name__}"
        raise
    finally:
        problems = [f"{summary[k]} {k}" for k in ("failed", "invalid", "unread") if summary[k]]
        if error or problems:
            _alert_recorder(error or "recordings not staged", ", ".join(problems) or "see logs")
        try:
            rds.eval(_RELEASE_LOCK_LUA, 1, _CHELAN_LOCK_KEY, token)
        except Exception as exc:  # noqa: BLE001 -- the lock expires on its own; never mask the run
            _logger.warning("Chelan NTS lock release failed: %s", type(exc).__name__)
        finally:
            rds.close()


def _bounded_fetch(url: str, max_bytes: int, wall_s: float, headers: dict) -> bytes:
    """One recorder download: SSRF-guarded, no redirects, byte-capped and time-boxed
    (wall clock, not just per-read), through a temp file removed afterwards."""
    import os
    import tempfile

    from src.utils.safe_http import safe_download_to_file

    fd, path = tempfile.mkstemp(suffix=".bin")
    os.close(fd)
    try:
        safe_download_to_file(url, path, max_bytes=max_bytes, require_https=True,
                              follow_redirects=False, headers=headers, timeout=60,
                              deadline_s=wall_s)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _alert_recorder(what: str, detail: str) -> None:
    from src.workers.ops_alerts import send_ops_alert

    send_ops_alert(
        kind="nts_recorder", key="nts_recorder:chelan",
        subject=f"Chelan NTS recorder crawl: {what}",
        body=(f"The Chelan County NTS recorder crawl: {what} ({detail}).\n\n"
              "Staged notices await operator review; nothing reaches customers from here. "
              "Check the crawler logs and nts_recorder_documents."),
    )
