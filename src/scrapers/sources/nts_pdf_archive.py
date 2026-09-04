"""Where a Pacific Publishing paper's BACK ISSUES live.

The legal-notices page links only the current issue, so nothing in the product could
reach last week's PDF — a missed or failed crawl lost that week's notices for good.
The back issues are unlinked but public and addressable: the filename IS the issue
date. This module holds that mapping, and only that.

It exists as a module rather than as constants inside either caller because there are
two callers with different jobs and they must not drift:

  * scripts/backfill_nts_pdf_archive.py — the one-shot operator recovery, which probes
    EVERY day across a wide window because it is trying to miss nothing.
  * src/workers/nts_crawler.py — the daily beat's bounded self-heal, which probes only
    the recent issue weekdays because it runs every day and only has to catch up.

Both ask this module for filenames, so a paper renaming its files is one edit here.
"""
from __future__ import annotations

import urllib.parse
from datetime import date, timedelta

# The CDN both papers publish to. Imported by callers so the SSRF allowlist and the
# crawler's host check stay in step with the URLs built here.
PDF_HOST = "pacificpublishingcompany.media.clients.ellingtoncms.com"


def king_names(d: date) -> list[str]:
    """King: "QA Legals MM-DD-YY.pdf" — zero-padded (verified across 18 issues)."""
    return [f"QA Legals {d.month:02d}-{d.day:02d}-{d.year % 100:02d}.pdf"]


def snoho_names(d: date) -> list[str]:
    """Snohomish: the paper keeps changing its own spelling, so every observed variant
    is probed in newest-convention-first order and the first hit wins.

    "Legals - M-D-YY.pdf" covers the back catalogue (6-10, 6-17, 6-24, 7-22, 8-5, 8-27).
    "Legals M-D-YY.pdf" — no separator — is what it switched to by 2026-09-02, and was
    missing here, so the newest issues were unreachable by construction. The padded
    forms cost one extra 404 on a miss and cover a future flip back.
    """
    m, dd, yy = d.month, d.day, d.year % 100
    return [
        f"Legals - {m}-{dd}-{yy:02d}.pdf",
        f"Legals {m}-{dd}-{yy:02d}.pdf",
        f"Legals - {m:02d}-{dd:02d}-{yy:02d}.pdf",
        f"Legals {m:02d}-{dd:02d}-{yy:02d}.pdf",
    ]


# prefix/county/names per source. `weekday` is the issue day (Mon=0) the weekly beat
# sweep anchors on — both papers date their issues Wednesday, though Snohomish has
# occasionally slipped a day (2026-08-27), which is why the wide operator backfill
# walks every date and this anchor is only the cheap recurring net.
ARCHIVE_SOURCES: dict[str, dict] = {
    "queen_anne_news": {
        "prefix": "/static-4/queenannenews/images/legals/",
        "county": "king",
        "names": king_names,
        "weekday": 2,
    },
    "snohomish_tribune": {
        "prefix": "/static-4/snoho/images/",
        "county": "snohomish",
        "names": snoho_names,
        "weekday": 2,
    },
}


def issue_url(source: str, name: str) -> str:
    """Full CDN URL for one issue filename (percent-encoded — the names carry spaces)."""
    return f"https://{PDF_HOST}{ARCHIVE_SOURCES[source]['prefix']}{urllib.parse.quote(name)}"


def recent_issue_dates(source: str, today: date, weeks: int) -> list[date]:
    """The last `weeks` issue dates for a paper, OLDEST FIRST.

    Oldest first is load-bearing, not cosmetic: _upsert_notice refreshes every mutable
    field ON CONFLICT, and a notice republished across issues can legitimately change
    between them (an inline "SALE POSTPONED TO <later date>"). Ingesting newest-first
    would let a stale older issue overwrite current truth, so the newest issue must
    always be the last writer.
    """
    weekday = ARCHIVE_SOURCES[source]["weekday"]
    newest = today - timedelta(days=(today.weekday() - weekday) % 7)
    return [newest - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


# Days AFTER the nominal issue weekday to also probe. The Snohomish Tribune has slipped
# an issue by a day (2026-08-27 was a Thursday), and a slipped issue in a week the crawl
# also missed would otherwise be unrecoverable by this route (Codex P3). Probed only for
# issues that are actually due, and only after the nominal day misses, so a healthy
# cache pays nothing.
_SLIP_DAYS = (0, 1)


def candidate_urls(source: str, today: date, weeks: int) -> list[list[str]]:
    """Per issue date (oldest first), the candidate URLs to try in order.

    Within one issue the order is: nominal day's spellings first, then the slipped
    day's. The caller stops at the first URL that actually downloads.
    """
    cfg = ARCHIVE_SOURCES[source]
    out = []
    for d in recent_issue_dates(source, today, weeks):
        urls = []
        for slip in _SLIP_DAYS:
            for n in cfg["names"](d + timedelta(days=slip)):
                url = issue_url(source, n)
                if url not in urls:
                    urls.append(url)
        out.append(urls)
    return out
