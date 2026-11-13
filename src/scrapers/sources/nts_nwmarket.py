"""Asotin County NTS ingestion adapter: Lewiston Tribune legal notices on nwmarket.com.

WA law (RCW 61.24.040) requires each Notice of Trustee Sale to be published in a legal
newspaper of the county. Asotin's run in the Lewiston Tribune, whose legal notices are
hosted on iPublish MarketPlace (www.nwmarket.com), ONE notice per PDF at
``/marketplace/image/notices/<id>/<id>_pdf.pdf``, each linked from an advert page
``/marketplace/advert/-Notices_<n>``. The feed mixes Idaho and Washington notices from
several counties (probate, bids, hearings, summons) with the occasional trustee sale.

Discovery: robots.txt disallows ``/marketplace/search/`` (the listing's only pagination),
so the full set comes from the published sitemap, whose per-advert ``<lastmod>`` lets a
run take just the recent adverts. robots.txt asks for a 20 s crawl delay; the crawler
honours it on every request.

Completeness (as nts_columbian): every recent advert's PDF is parsed. The listing
snippet is too short to pre-filter safely, so ``is_valid_nts`` + ``is_asotin_property``
are the backstops.

Pure functions, no network; the crawler injects the fetcher.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlparse

BASE_URL = "https://www.nwmarket.com"
SITEMAP_URL = BASE_URL + "/marketplace/sitemap.xml"
CRAWL_DELAY_S = 20.0  # robots.txt Crawl-Delay
# nts_notices.source is varchar(32) and part of the natural key.
SOURCE = "lmtribune_nwmarket"
COUNTY = "asotin"
_HOST = "www.nwmarket.com"

_SITEMAP_ENTRY = re.compile(
    r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*(\d{4}-\d{2}-\d{2})[^<]*</lastmod>)?",
    re.I,
)
_ADVERT_PATH = re.compile(r"^/marketplace/advert/-Notices_\d+$")
_PDF_HREF = re.compile(r"""href=["'](/marketplace/image/notices/(\d+)/\2_pdf\.pdf)["']""")
# Where the PROPERTY is, never just where the sale is held or a court sits: the
# statutory "situated in the County of Asotin, State of Washington" and the recording
# reference "records of Asotin County, Washington" (both seen on live 2026 notices).
_ASOTIN_PROPERTY = re.compile(
    r"situated\s+in\s+the\s+County\s+of\s+Asotin\s*,\s*State\s+of\s+Washington"
    r"|records\s+of\s+Asotin\s+County\s*,\s*(?:State\s+of\s+)?Washington",
    re.I,
)


def recent_adverts(sitemap_xml: str, since: date) -> list[tuple[str, str]]:
    """``(advert_url, lastmod)`` for host-pinned adverts changed on or after ``since``.

    ``lastmod`` is the advert's version: a postponement republished at the same URL
    gets a new one, so a crawler that remembers (url, lastmod) re-reads it. The live
    sitemap repeats some adverts; the latest lastmod wins. An entry with no lastmod is
    kept with version "" (we cannot tell it is old). Sitemap order.
    """
    out: dict[str, str] = {}
    for loc, lastmod in _SITEMAP_ENTRY.findall(sitemap_xml):
        parsed = urlparse(loc)
        if parsed.hostname != _HOST or not _ADVERT_PATH.match(parsed.path):
            continue
        if lastmod and date.fromisoformat(lastmod) < since:
            continue
        # The site 301s "-Notices_N" to "-notices_N"; ask for the canonical form so a
        # fetch is one request, not two inside the crawl delay (seen live 2026-11-12).
        url = f"https://{_HOST}{parsed.path.lower()}"
        out[url] = max(out.get(url, ""), lastmod)
    return list(out.items())


def extract_pdf_url(advert_html: str) -> str | None:
    """The notice PDF linked from an advert page, absolute on our host, or None."""
    m = _PDF_HREF.search(advert_html)
    return urljoin(BASE_URL, m.group(1)) if m else None


def is_asotin_property(notice_text: str) -> bool:
    """True when the notice's real property lies in Asotin County, Washington."""
    return bool(_ASOTIN_PROPERTY.search(notice_text))
