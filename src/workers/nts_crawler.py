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

import time
from datetime import UTC, datetime, timedelta

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
# (that's the DJC, $350/yr, deferred). Same weekly-PDF pipeline; its legals live in a
# /legals/ subdir (vs snoho's flat /images/).
_KING_PAGE = "https://queenannenews.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498"
_KING_PDF_PREFIX = "/static-4/queenannenews/images/legals/"
_KING_SOURCE = "queen_anne_news"

# ── The Columbian classifieds (Clark County) ──
# Clark County trustee sales publish free/open (robots.txt 404) on The Columbian's
# classifieds site — a single rolling HTML listing (no pagination) mixing every current
# legal notice. Ingestion adapter: src/scrapers/sources/nts_columbian.py. Unlike Tacoma
# (one NTS-slug page per notice) we fetch EVERY ad permalink and let is_valid_nts
# backstop — the listing preview is truncated, so pre-filtering could silently drop a
# lead (Codex Q1). Browser UA (some classifieds gate non-browser UAs); these are public
# statutory RCW 61.24.040 notices.
_CLARK_MAX_ADS = 60  # hard cap on ad-detail fetches per run (the listing is ~32)


@app.task(name="src.workers.nts_crawler.crawl_nts_tacoma_index")
def crawl_nts_tacoma_index() -> dict:
    """Crawl + upsert recent Tacoma Daily Index NTS notices. Returns a summary."""
    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_get

    today = datetime.now(UTC).date()

    def _fetch_listing(page: int):
        """I/O for one listing page: returns (status_code, html) or None on failure.

        A transient failure returns None so collect_notice_urls skips just this page
        (a later page may still carry trustee sales) instead of abandoning the crawl.
        """
        if page > 1:
            time.sleep(_LISTING_DELAY_S)  # polite: we now walk multiple pages every run
        url = nts.BASE_URL + nts.LEGAL_NOTICES_PATH + (f"page/{page}/" if page > 1 else "")
        try:
            resp = safe_get(url, timeout=20, headers={"User-Agent": "BridgeLeadsBot/1.0"})
        except Exception as exc:  # noqa: BLE001 — a bad page must not kill the crawl
            _logger.warning("NTS listing page %d fetch failed: %s", page, str(exc)[:120])
            return None
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
                _upsert_notice(db, NtsNotice, row)
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
    return _crawl_pacific_publishing_pdf(
        page_url=_SNOHO_PAGE,
        pdf_path_prefix=_SNOHO_PDF_PREFIX,
        source=_SNOHO_SOURCE,
        county="snohomish",
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
                    _upsert_notice(db, NtsNotice, row)
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


def _crawl_pacific_publishing_pdf(
    *, page_url: str, pdf_path_prefix: str, source: str, county: str, parse_fn=None
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
    import os
    import tempfile

    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_pdf
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_download_to_file

    # Parser variance is isolated per paper (Codex): Snohomish uses the shared
    # colon parser; King passes parse_king_notice for its no-colon/surrogate-key
    # layouts. Default preserves existing (Snohomish/Tacoma) behavior.
    if parse_fn is None:
        parse_fn = nts.parse_nts_notice

    today = datetime.now(UTC).date()
    summary = {"source": source, "pdf_url": None, "blocks": 0,
               "upserted": 0, "skipped": 0, "errored": 0, "expired": 0}

    pdf_url = _discover_latest_legals_pdf(page_url, pdf_path_prefix)
    summary["pdf_url"] = pdf_url
    if not pdf_url:
        _logger.warning("NTS PDF crawl (%s): no current legals PDF found", source)
        _alert_if_crawl_barren(source, discovered=0, upserted=0)
        return summary

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        try:
            safe_download_to_file(
                pdf_url, path, max_bytes=_MAX_PDF_BYTES, require_https=True,
                headers={"User-Agent": _PDF_BROWSER_UA},
            )
            with open(path, "rb") as fh:
                data = fh.read()
            text = nts_pdf.extract_pdf_text(data)
        except Exception as exc:  # noqa: BLE001 — a bad download/PDF must not crash the beat
            summary["errored"] += 1
            _logger.warning("NTS PDF download/extract failed (%s): %s", pdf_url, str(exc)[:160])
            return summary
        blocks = nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(text))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    summary["blocks"] = len(blocks)
    with system_sync_session() as db:
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
                # Per-row SAVEPOINT: one bad notice (e.g. an over-long field) rolls back
                # alone instead of poisoning the whole PDF's batch (Codex: failure isolation).
                with db.begin_nested():
                    _upsert_notice(db, NtsNotice, row)
                summary["upserted"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errored"] += 1
                _logger.warning("NTS PDF notice upsert failed (%s): %s", source, str(exc)[:140])
        db.commit()

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

    _logger.info("NTS PDF crawl done (%s): %s", source, summary)
    _alert_if_crawl_barren(source, discovered=summary["blocks"], upserted=summary["upserted"])
    return summary


_NO_UPDATE = frozenset({"id", "source", "ts_number", "created_at"})


def _upsert_notice(db, model, row: dict) -> None:
    """Upsert one notice on (source, ts_number); refresh all mutable fields.

    Idempotent: a re-crawl rewrites the mutable fields. id (PK) is set only on the
    INSERT path and is NEVER in the conflict-update set — refreshing an existing
    notice must not churn its PK or created_at. The natural key (source, ts_number)
    and id are stable across re-crawls.
    """
    from uuid import uuid4

    row = {**row, "id": str(uuid4())}
    stmt = pg_insert(model).values(**row)
    update_cols = {c: stmt.excluded[c] for c in row if c not in _NO_UPDATE}
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


_RESWEEP_SELECT = _sa_text(
    """
    SELECT id, ts_number, source_url FROM nts_notices
    WHERE source = :source AND is_active AND auction_date >= :today
      AND principal_owing IS NULL AND source_url IS NOT NULL
      AND (fetched_at IS NULL OR fetched_at < :stale_before)
    ORDER BY auction_date ASC
    LIMIT :lim
    """
)
_TOUCH_FETCHED = _sa_text("UPDATE nts_notices SET fetched_at = :now WHERE id = :id")


def _resweep_null_amount_notices(
    db, model, *, source: str, county: str, today, fetch, parse, notice_to_row,
) -> dict:
    """Re-fetch a bounded batch of active, future-dated notices with NO amount.

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
            row = notice_to_row(parse(html), source_url=r.source_url, today=today,
                                source=source, county=county)
            if row is None or row.get("principal_owing") is None:
                counts["unchanged_null"] += 1
                db.execute(_TOUCH_FETCHED, {"now": now, "id": r.id})
                continue
            row["fetched_at"] = now
            _upsert_notice(db, model, row)
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
        return "0 notices discovered — listing/PDF discovery may have broken"
    if alert_on_zero_upserts and upserted == 0:
        return f"{discovered} notices discovered but 0 upserted — parser may have broken"
    if not alert_on_zero_upserts and upserted == 0 and errored >= discovered:
        return (
            f"{discovered} ads discovered but all {errored} detail fetches failed — "
            "source may be down or blocking"
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


def _td_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)
