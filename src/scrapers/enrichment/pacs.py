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


def parse_pacs_result_html(html_text: str) -> dict | None:
    """Parse a PACS PropertyAccess search-results page into {address, mailing, value}.

    Over-inference guard (Codex point C). An owner-name search can match MANY
    properties; the old parser flattened ALL result rows' cells and trusted the
    first parcel/address it saw — silently picking row 1 of an ambiguous match.
    An owner-name match is WEAK evidence, so:
      1. Require EXACTLY ONE plausible result row in resultsTable; on 0 or >1,
         return None (we can't know which property is the filing party's).
      2. NEVER return parcel_id from this path. parcel_id is identity/billing/
         dedup input (``compute_property_key`` is parcel-primary, and the FROZEN
         ``legacy_strong_signature`` keys billing dedup) — a name-derived parcel
         could corrupt cross-list overlap. The PACS columns are
         ``checkbox, account, parcel, ...`` (account AND parcel are both long
         numbers), so the old "first 10+ digit cell" even risked storing the
         ACCOUNT as the parcel. Address/mailing still hydrate (the feature's
         purpose: unlock skip-trace on probate estate filings).

    Pure function (no HTTP) so the guard is unit-testable. Returns None on no
    usable single-row address.
    """
    table_start = html_text.find("resultsTable")
    if table_start == -1:
        return None
    table_end = html_text.find("</table>", table_start)
    chunk = (html_text[table_start:table_end]
             if table_end > table_start
             else html_text[table_start:table_start + 5000])

    def _row_cells(row_html: str) -> list[str]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL | re.IGNORECASE)
        cleaned = [re.sub(r"<[^>]+>", " ", td).strip().replace("&nbsp;", "").strip()
                   for td in tds]
        return [c for c in cleaned if c]

    # Count only PLAUSIBLE result rows, not "any <tr> with a <td>" (Codex P2): a
    # real PACS result row has ~10 columns INCLUDING long account/parcel numbers.
    # Filtering on (>=5 cells AND a 6+ digit number) before the uniqueness check
    # means a stray pager/footer row, or a header rendered with <td> instead of
    # <th>, can't turn a single genuine match into a false miss. Case-insensitive
    # so an uppercase-tag portal isn't mis-read as zero rows.
    candidate_rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", chunk, re.DOTALL | re.IGNORECASE):
        cells = _row_cells(row)
        if len(cells) >= 5 and any(re.search(r"\d{6,}", c) for c in cells):
            candidate_rows.append(cells)
    if len(candidate_rows) != 1:
        return None
    cells = candidate_rows[0]

    result: dict[str, str] = {}
    for cell in cells:
        cell_clean = cell.replace("\r\n", "\n").replace("\r", "\n")
        # Address: number + street, possibly with city/state on the next line.
        if re.search(r"\d+\s+[A-Z].*WA\s+\d{5}", cell_clean, re.I | re.DOTALL):
            lines = [ln.strip() for ln in cell_clean.split("\n") if ln.strip()]
            result["address"] = lines[0]
            if len(lines) > 1:
                result["mailing"] = ", ".join(lines)
        elif cell.startswith("$") and "value" not in result:
            result["value"] = cell

    # parcel_id intentionally NOT extracted from owner-name search (see above).
    return result if result.get("address") else None


def lookup_pacs_by_name(pacs_url: str, owner_name: str) -> dict | None:
    """Search a PACS PropertyAccess portal by owner name.

    Returns a dict with any of: address, mailing, value (NEVER parcel_id — an
    owner-name match is weak evidence; see ``parse_pacs_result_html``).
    Returns None on no unique match or error.

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

        return parse_pacs_result_html(r.text)
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
