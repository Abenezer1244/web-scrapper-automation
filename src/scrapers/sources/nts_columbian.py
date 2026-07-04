"""Clark County (The Columbian classifieds) NTS ingestion adapter.

WA law (RCW 61.24.040) requires every Notice of Trustee Sale to be published in a
county legal newspaper. The Columbian (Clark County's paper) publishes them free and
open (robots.txt 404 — no restriction) at classifieds.columbian.com under the "Legal
Notices" subcategory. Unlike the Tacoma Daily Index (one HTML page per notice) or the
Pacific Publishing weekly PDFs, The Columbian serves ONE rolling listing page (no
pagination) that MIXES every current legal notice (court summons, probate, RFP/bids,
and the occasional trustee sale). Each ad links to a permalink ``/ad-details/<id>``
whose full body lives in ``<p class="ad-content-container">``.

This module is the INGESTION ADAPTER only (like ``nts_king_pdf`` / ``nts_pdf``): pull
the ad permalinks off the listing and extract each ad's body text. The per-notice field
parsing is REUSED from the shared ``nts_tacoma_index.parse_tacoma_notice`` (Clark
notices are the statutory Quality Loan / MTC layouts it already handles — the MTC
dual-label "Original Trustee … Current Trustee …" fix lives in that shared parser
because it is a general WA layout bug, not Clark-specific). The crawler then runs each
body through the shared parser + ``notice_to_row(source=SOURCE, county=COUNTY)``.

Completeness (Codex): the crawler fetches EVERY ad permalink and lets ``is_valid_nts``
be the backstop — it never pre-filters the listing preview to decide what to skip, so a
trustee sale whose truncated preview happens not to contain the word "trustee" can never
be silently dropped. The listing is ~32 ads; fetching all daily is trivial next to the
cost of one missed (and silent) foreclosure lead.

Pure functions, no network — the crawler injects the fetcher and these are unit-tested
against REAL saved fixtures (no mocks).
"""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

BASE_URL = "https://classifieds.columbian.com"
LISTING_PATH = "/subcategories/view/55"  # "Legal Notices" subcategory
# source is the nts_notices natural-key prefix and is varchar(32) — keep it short.
SOURCE = "columbian_classifieds"
COUNTY = "clark"
STATE = "WA"
_HOST = "classifieds.columbian.com"


def extract_ad_detail_urls(listing_html: str) -> list[str]:
    """Pull the distinct ``/ad-details/<id>`` permalinks from the legal-notices listing.

    Returns ABSOLUTE, host-pinned URLs in listing order (dedup preserved). We do NOT
    filter to trustee sales here (Codex Q1): the listing preview is truncated and a real
    NTS can start with a TS# / trustee-company header rather than the word "trustee", so
    pre-filtering could silently drop a lead. Every ad is fetched and ``is_valid_nts``
    (after parsing the full body) is the backstop that discards non-NTS notices.

    Host-pinned to ``classifieds.columbian.com`` (defense-in-depth with the crawler's
    ``same_origin_as``): a syndicated/absolute off-host ``/ad-details`` link is dropped.
    """
    soup = BeautifulSoup(listing_html, "lxml")
    seen: list[str] = []
    for a in soup.select("a.view_post[href], a[href*='/ad-details/']"):
        href = a.get("href", "")
        if "/ad-details/" not in href:
            continue
        url = urljoin(BASE_URL + "/", href)
        parsed = urlparse(url)
        # keep only numeric ad-details paths on our host
        tail = parsed.path.rsplit("/ad-details/", 1)[-1]
        if parsed.hostname != _HOST or not tail.isdigit():
            continue
        if url not in seen:
            seen.append(url)
    return seen


def extract_ad_body(ad_html: str) -> str:
    """Return the notice body text from an ``/ad-details/<id>`` page for the parser.

    The body lives in ``<p class="ad-content-container">``. We drop script/style/nav
    chrome, convert block structure to newlines (so adjacent labels never concatenate),
    and normalize whitespace — the same shape the shared parser expects. If more than one
    content container exists (defensive), they are joined in document order.
    """
    soup = BeautifulSoup(ad_html, "lxml")
    for junk in soup.select("script, style, noscript"):
        junk.decompose()
    parts = [
        p.get_text(separator="\n", strip=True)
        for p in soup.select("p.ad-content-container")
    ]
    body = "\n".join(part for part in parts if part)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return "\n".join(lines)
