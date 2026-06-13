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

_MAX_PAGES = 5            # listing pages per run (newest first)
_MAX_NOTICES = 200        # hard cap on fetches per run
_FETCH_DELAY_S = 1.0      # polite delay between notice fetches
_CACHE_DAYS = 90          # expire notices not seen in this window


@app.task(name="src.workers.nts_crawler.crawl_nts_tacoma_index")
def crawl_nts_tacoma_index() -> dict:
    """Crawl + upsert recent Tacoma Daily Index NTS notices. Returns a summary."""
    from src.db.models import NtsNotice
    from src.db.session import system_sync_session
    from src.scrapers.sources import nts_tacoma_index as nts
    from src.utils.safe_http import safe_get

    today = datetime.now(UTC).date()
    notice_urls: list[str] = []
    for page in range(1, _MAX_PAGES + 1):
        url = nts.BASE_URL + nts.LEGAL_NOTICES_PATH + (f"page/{page}/" if page > 1 else "")
        try:
            resp = safe_get(url, timeout=20, headers={"User-Agent": "BridgeLeadsBot/1.0"})
        except Exception as exc:  # noqa: BLE001 — a bad page must not kill the crawl
            _logger.warning("NTS listing page %d fetch failed: %s", page, str(exc)[:120])
            continue
        if resp.status_code != 200:
            _logger.info("NTS listing page %d -> HTTP %d; stopping pagination", page, resp.status_code)
            break
        found = nts.extract_notice_urls(resp.text)
        if not found:
            break  # no more notices on this/later pages
        for u in found:
            if u not in notice_urls:
                notice_urls.append(u)
        if len(notice_urls) >= _MAX_NOTICES:
            break

    notice_urls = notice_urls[:_MAX_NOTICES]
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
                parsed = nts.parse_nts_notice(nts.extract_article_text(resp.text))
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


def _td_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)
