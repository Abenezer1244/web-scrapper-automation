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
from datetime import UTC, datetime

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
               "skipped": skipped, "errored": errored, "expired": expired or 0}
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
                    summary["skipped"] += 1  # not a parseable NTS body (commercial/other format)
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


def _barren_alert_reason(discovered: int, upserted: int) -> str | None:
    """Pure decision: the reason string to alert on a barren crawl, or None if healthy.

    discovered == 0 -> discovery broke; discovered > 0 but upserted == 0 -> parse broke;
    any upsert -> healthy (no alert).
    """
    if discovered == 0:
        return "0 notices discovered — listing/PDF discovery may have broken"
    if upserted == 0:
        return f"{discovered} notices discovered but 0 upserted — parser may have broken"
    return None


def _alert_if_crawl_barren(source: str, *, discovered: int, upserted: int) -> None:
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
    reason = _barren_alert_reason(discovered, upserted)
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
