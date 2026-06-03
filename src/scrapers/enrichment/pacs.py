"""Tyler PACS PropertyAccess name-based lookup.

PACS (Property Appraisal / Collection System) PropertyAccess is a Tyler
Technologies product used by many WA counties for public property search.
Portals like Chelan, Douglas, Pend Oreille, and Island all run PACS at URLs
like https://<host>/propertyaccess/?cid=<N>.

This module isolates the HTTP-only name-based search: given a PACS URL and
an owner name, return the matching property address, parcel ID, and mailing
address. Pattern extracted from src/scrapers/templates/acclaimweb.py so it
can be reused from the post-scrape enrichment pipeline (workers/tasks.py)
for ANY county whose connector has assessor_url set to a PACS portal.

No browser, no AI — it's an ASP.NET page with VIEWSTATE/EVENTVALIDATION
tokens posted back to the same URL.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from src.api.middleware.security import validate_scraping_target
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.pacs")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
}


def is_pacs_url(url: str | None) -> bool:
    """Return True if url looks like a Tyler PACS PropertyAccess portal."""
    if not url:
        return False
    low = url.lower()
    return "/propertyaccess" in low or "propertyaccess/" in low


def lookup_pacs_by_name(pacs_url: str, owner_name: str) -> dict | None:
    """Search a PACS PropertyAccess portal by owner name.

    Returns a dict with any of: address, parcel_id, mailing, value.
    Returns None on no match or error.

    Blocks on HTTP; call from a thread pool when batching.
    """
    if not pacs_url or not owner_name:
        return None

    # N1: pacs_url is DB config (CountyConnector.assessor_url). An operator
    # could point it at an internal host, or a PACS host could 302 internally.
    # Validate with resolve=True (DNS-rebinding aware) BEFORE any outbound
    # request, and refuse plaintext (PACS portals are HTTPS). raise -> caught
    # by the except below and logged as a failed lookup (returns None).
    if urlparse(pacs_url).scheme != "https":
        _logger.warning("PACS lookup refused non-HTTPS assessor_url")
        return None
    validate_scraping_target(pacs_url, require_allowlisted=False, resolve=True)

    # Island PACS responses run 10-18s on estate-name searches — bumped
    # timeouts + one retry on read timeout recovers ~2x the records
    # that the original 10/12s budget was dropping.
    _GET_TIMEOUT = 20
    _POST_TIMEOUT = 25

    def _do_request():
        sess = requests.Session()
        sess.headers.update(_HEADERS)
        # N1: trust_env=False disables ambient HTTP(S)_PROXY so the request
        # can't be rerouted off-box after the SSRF check; allow_redirects=False
        # on both hops so a poisoned 302 can't bounce to an internal/metadata
        # host. PACS posts back to the same URL — no legitimate redirect.
        sess.trust_env = False
        r0 = sess.get(pacs_url, timeout=_GET_TIMEOUT, allow_redirects=False)
        if r0.status_code != 200:
            return None, None
        vs = re.search(r'__VIEWSTATE.*?value="([^"]+)"', r0.text)
        ev = re.search(r'__EVENTVALIDATION.*?value="([^"]+)"', r0.text)
        vsg = re.search(r'__VIEWSTATEGENERATOR.*?value="([^"]+)"', r0.text)
        if not vs:
            return None, None
        data = {
            "__VIEWSTATE": vs.group(1),
            "__EVENTVALIDATION": ev.group(1) if ev else "",
            "__VIEWSTATEGENERATOR": vsg.group(1) if vsg else "",
            "propertySearchOptions$ownerName": owner_name,
            "propertySearchOptions$search": "Search",
        }
        r = sess.post(pacs_url, data=data, timeout=_POST_TIMEOUT, allow_redirects=False)
        return sess, r

    try:
        sess, r = None, None
        for attempt in range(2):  # one retry on read timeout
            try:
                sess, r = _do_request()
                break
            except requests.exceptions.ReadTimeout:
                if attempt == 1:
                    raise
        if r is None or r.status_code != 200 or "None found" in r.text:
            return None

        table_start = r.text.find("resultsTable")
        if table_start == -1:
            return None
        table_end = r.text.find("</table>", table_start)
        chunk = (r.text[table_start:table_end]
                 if table_end > table_start
                 else r.text[table_start:table_start + 5000])

        tds = re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.DOTALL)
        cells = [re.sub(r"<[^>]+>", " ", td).strip().replace("&nbsp;", "").strip()
                 for td in tds]
        cells = [c for c in cells if c]
        if len(cells) < 5:
            return None

        result: dict[str, str] = {}
        for cell in cells:
            cell_clean = cell.replace("\r\n", "\n").replace("\r", "\n")
            # Parcel ID: 10+ digit contiguous number
            if re.match(r"^\d{10,}$", cell.replace(" ", "")) and "parcel_id" not in result:
                result["parcel_id"] = cell.replace(" ", "")
            # Address: number + street, possibly with city/state
            elif re.search(r"\d+\s+[A-Z].*WA\s+\d{5}", cell_clean, re.I | re.DOTALL):
                lines = [ln.strip() for ln in cell_clean.split("\n") if ln.strip()]
                result["address"] = lines[0]
                if len(lines) > 1:
                    result["mailing"] = ", ".join(lines)
            elif cell.startswith("$") and "value" not in result:
                result["value"] = cell

        if result.get("address") or result.get("parcel_id"):
            return result
        return None
    except Exception as exc:
        _logger.warning("PACS name lookup failed for %r: %s", owner_name[:30], str(exc)[:80])
        return None


def batch_lookup_pacs_by_name(
    pacs_url: str,
    owner_names: list[str],
    max_workers: int = 5,
) -> list[dict | None]:
    """Concurrent PACS name lookups. Returns one result (or None) per input name,
    in the same order as owner_names.
    """
    if not pacs_url or not owner_names:
        return [None] * len(owner_names)

    results: list[dict | None] = [None] * len(owner_names)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(lookup_pacs_by_name, pacs_url, name): i
            for i, name in enumerate(owner_names)
        }
        for fut in futures:
            i = futures[fut]
            try:
                results[i] = fut.result(timeout=30)
            except Exception:
                results[i] = None
    return results
