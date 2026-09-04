"""Recover NTS notices from weekly legals PDFs the crawler never fetched.

WHY THIS EXISTS
    The Pacific Publishing papers (Snohomish Tribune, Queen Anne & Magnolia News) expose
    only the CURRENT issue on their legal-notices page — there is no archive link — and
    until 2026-09-03 both crawls ran THURSDAYS ONLY. One missed or failed Thursday lost
    that week's notices permanently, because nothing ever revisited them. Measured for
    King on 2026-09-03: only 4 of 14 published issues were ever ingested, the cache held
    14 notices where the back issues carry 31, and 8 of the missing ones were still-live
    auctions the product could not show.

    The back issues are unlinked but PUBLIC and fetchable by constructed URL (verified
    14/14 HTTP 200). These are statutory RCW 61.24.040 notices, robots.txt allows "*",
    and they live on the same CDN path the crawler already downloads — this script just
    asks for the issues the discovery page stopped linking. It fetches politely, caps
    what it will pull, and is dry-run by default.

    Going forward the beat runs DAILY (src/workers/scheduler.py), so this should be a
    one-time recovery per source rather than a recurring chore.

ORDER MATTERS — OLDEST ISSUE FIRST
    `_upsert_notice` refreshes every mutable field ON CONFLICT (source, ts_number). A
    notice republished across several issues can legitimately CHANGE between them (an
    inline "SALE POSTPONED TO <later date>" is the common case). Ingesting newest-first
    would let a stale older issue overwrite the current truth, so this walks strictly
    oldest -> newest and the most recent issue always writes last.

CONCURRENCY WITH THE DAILY BEAT
    Oldest -> newest only holds for a single uninterrupted writer (Codex). If the daily
    crawl lands mid-run, an older issue here can briefly overwrite the fresher row the
    beat just wrote. It is self-limiting — the beat re-ingests the CURRENT issue every
    day, so any staleness clears within 24h — but for a clean run, apply this while the
    two PDF beat tasks are paused, or simply re-run the crawl task afterwards.

RELATIONSHIP TO scripts/repair_nts_ts_number.py
    They are complementary and this one runs FIRST. Backfill CREATES the correctly-keyed
    rows (with the fixed parser); the repair script RETIRES the mis-bound rows earlier
    crawls wrote under the wrong TS number. Run backfill --apply, then the repair's dry
    run, then the repair --apply.

Usage:
    railway run --service worker python scripts/backfill_nts_pdf_archive.py --source queen_anne_news
    railway run --service worker python scripts/backfill_nts_pdf_archive.py --source queen_anne_news --apply
    # widen/narrow the window (default: 90 days back from today)
    ... --source snohomish_tribune --days 120 --apply

Dry-run by default: prints every issue found and every notice it would upsert, writes nothing.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse
from datetime import UTC, date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db.session import system_sync_session  # noqa: E402
from src.scrapers.sources import nts_pdf  # noqa: E402
from src.scrapers.sources import nts_tacoma_index as nts  # noqa: E402
from src.utils.logger import setup_logger  # noqa: E402
from src.utils.lead_signals import auction_reference_date  # noqa: E402
from src.utils.safe_http import safe_get  # noqa: E402
from src.workers.nts_crawler import (  # noqa: E402
    _MAX_PDF_BYTES,
    _PDF_BROWSER_UA,
    _PDF_HOST,
    _upsert_notice,
)

_logger = setup_logger("scripts.backfill_nts_pdf_archive")

_CDN = f"https://{_PDF_HOST}"
_FETCH_DELAY_S = 0.4      # polite gap between probes
_MAX_ISSUES = 40          # abort guard (NOT a truncation) — see the probe loop


# Each source: CDN path prefix, county, filename builder, and the parser its crawler
# uses. King MUST use parse_king_notice — its no-colon Affinia fields and surrogate
# REF-/APN- keys come out as garbage under the shared colon parser.
def _sources() -> dict:
    """Per-source config = the shared archive map + the parser this paper needs.

    The prefix/county/filename rules live in src/scrapers/sources/nts_pdf_archive.py so
    this one-shot recovery and the crawler's daily self-heal sweep cannot disagree about
    where a back issue lives (they did: this script's Snohomish builder only knew the
    "Legals - M-D-YY.pdf" spelling and could not reach the no-separator names the paper
    switched to by 2026-09-02).
    """
    from src.scrapers.sources.nts_king_pdf import parse_king_notice
    from src.scrapers.sources.nts_pdf_archive import ARCHIVE_SOURCES

    parsers = {
        "queen_anne_news": parse_king_notice,
        "snohomish_tribune": nts.parse_nts_notice,
    }
    return {name: {**cfg, "parse": parsers[name]} for name, cfg in ARCHIVE_SOURCES.items()}


def _try_fetch(url: str) -> bytes | None:
    """GET one candidate issue. A 404 is the NORMAL answer for a day with no issue and
    must never abort the run — unlike repair_nts_ts_number, an unreachable URL here is
    an absence of data, not an incomplete truth map."""
    try:
        resp = safe_get(url, timeout=45, headers={"User-Agent": _PDF_BROWSER_UA})
    except Exception as exc:  # noqa: BLE001
        _logger.info("probe failed %s: %s", url.rsplit("/", 1)[-1], str(exc)[:100])
        return None
    if resp.status_code != 200:
        return None
    data = resp.content
    if len(data) > _MAX_PDF_BYTES:
        _logger.warning("skipping oversized PDF (%d bytes): %s", len(data), url)
        return None
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, choices=sorted(_sources()))
    ap.add_argument("--days", type=int, default=90, help="lookback window (default 90)")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    args = ap.parse_args()

    cfg = _sources()[args.source]
    # County-local, matching trustee_sale's own filter (Codex): a UTC clock in the
    # Pacific evening rolls over early and would mark a same-day WA auction inactive.
    today = auction_reference_date()
    start = today - timedelta(days=args.days)

    # OLDEST FIRST — see the module docstring. The newest issue must write last.
    print(f"Probing {args.source} issues from {start} to {today} (oldest first)…")
    issues: list[tuple[date, str, bytes]] = []
    d = start
    while d <= today:
        for name in cfg["names"](d):
            url = _CDN + cfg["prefix"] + urllib.parse.quote(name)
            data = _try_fetch(url)
            time.sleep(_FETCH_DELAY_S)
            if data:
                issues.append((d, url, data))
                print(f"  found {name} ({len(data)} bytes)")
                break
        d += timedelta(days=1)

    print(f"\n{len(issues)} issue PDF(s) found.\n")
    if not issues:
        print("Nothing to do.")
        return
    # ABORT rather than truncate (Codex High). The cap used to stop the probe loop, which
    # would have applied the OLDEST prefix and never reached the newest issues — and since
    # the upsert refreshes every mutable field, a stale older auction date would then be
    # the final truth. Narrowing --days is the operator's call, not a silent one.
    if len(issues) > _MAX_ISSUES:
        raise SystemExit(
            f"ABORT: {len(issues)} issues exceeds the {_MAX_ISSUES}-issue cap. Applying an "
            "oldest-first PREFIX would let a stale issue win the upsert. Re-run with a "
            "smaller --days (or raise _MAX_ISSUES deliberately)."
        )

    upserted = skipped = errored = 0
    with system_sync_session() as db:
        from src.db.models import NtsNotice

        for issue_date, url, data in issues:
            try:
                blocks = nts_pdf.split_notice_blocks(
                    nts_pdf.normalize_pdf_text(nts_pdf.extract_pdf_text(data))
                )
            except Exception as exc:  # noqa: BLE001
                errored += 1
                print(f"  {issue_date} PARSE FAILED: {str(exc)[:100]}")
                continue
            kept = 0
            for block in blocks:
                parsed = cfg["parse"](block)
                row = nts.notice_to_row(
                    parsed, source_url=url, today=today,
                    source=args.source, county=cfg["county"],
                )
                if row is None:
                    skipped += 1
                    continue
                row["fetched_at"] = datetime.now(UTC)
                print(f"    {row['ts_number']:22} parcel={str(row['parcel']):16} "
                      f"auction={row['auction_date']} active={row['is_active']} "
                      f"owing={row['principal_owing']}")
                if args.apply:
                    # The SAVEPOINT rolls one bad notice back, but it does NOT stop the
                    # exception escaping — which would abort the run before the NEWER
                    # issues write, i.e. exactly the ones that repair stale reposts
                    # (Codex). Catch, count, keep going.
                    try:
                        with db.begin_nested():
                            _upsert_notice(db, NtsNotice, row)
                    except Exception as exc:  # noqa: BLE001
                        errored += 1
                        print(f"      UPSERT FAILED {row['ts_number']} ({url}): {str(exc)[:110]}")
                        continue
                kept += 1
                upserted += 1
            print(f"  {issue_date}: {kept}/{len(blocks)} notices\n")

        if args.apply:
            db.commit()
            print(f"APPLIED — {upserted} notice(s) upserted, {skipped} non-NTS block(s) "
                  f"skipped, {errored} failure(s).")
            if upserted == 0:
                # Issues downloaded but nothing parsed out of them is the exact
                # parser-gap failure this whole effort exists to surface (Codex).
                raise SystemExit(
                    f"FAILED: {len(issues)} issue(s) downloaded but 0 notices upserted — "
                    "that is a parser gap, not an empty archive."
                )
        else:
            db.rollback()
            print(f"DRY RUN — {upserted} notice(s) would be upserted, {skipped} non-NTS "
                  f"block(s) skipped, {errored} issue(s) unreadable. Re-run with --apply.")


if __name__ == "__main__":
    main()
