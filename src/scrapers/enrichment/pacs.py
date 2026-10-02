"""Tyler PACS PropertyAccess name-based lookup.

PACS (Property Appraisal / Collection System) PropertyAccess is a Tyler
Technologies product used by many WA counties for public property search.
Portals like Chelan, Douglas, Pend Oreille, and Island all run PACS at URLs
like https://<host>/propertyaccess/?cid=<N>.

This module isolates the HTTP-only name-based search: given a PACS URL and
an owner name, return the matching property address and, from the property's
detail page, its owner mailing address. Pattern extracted from
src/scrapers/templates/acclaimweb.py so it can be reused from the post-scrape
enrichment pipeline (workers/tasks.py) for ANY county whose connector has
assessor_url set to a PACS portal. The parcel-keyed lookup that the mailing
enrichment uses lives in pacs_parcel.py and shares the detail-page parser here.

No browser, no AI — it's an ASP.NET page with VIEWSTATE/EVENTVALIDATION
tokens posted back to the same URL.

WHERE THE MAILING ADDRESS IS (verified on Benton's portal, 2026-10-02): the
search RESULTS grid has the columns Property ID, Parcel # / Geo ID, Type, Tax
Area, Property Address, Legal Description, Owner Name, Appraised Value. It has
NO mailing column. Only the detail page (Property.aspx?prop_id=N) carries a
labelled "Mailing Address:" cell, in the owner block, apart from the situs
"Address:" cell in the property block. Until 2026-10-02 this module built
``mailing`` out of the grid's address cell, which is the SITUS: every mailing
address it produced was the property address copied over, a fabricated
owner-occupancy fact. The grid parser no longer returns ``mailing`` at all.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.api.middleware.security import validate_scraping_target
from src.utils.logger import setup_logger
from src.utils.pinned_http import pinned_session

_logger = setup_logger("scraper.enrichment.pacs")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
}

# Detail-page labels, lower-cased and without the trailing colon. The parcel label
# varies by PACS version: Benton prints "Parcel # / Geo ID:", Clallam/Jefferson
# "Geographic ID:". "Address:" is the situs; "Mailing Address:" is the owner's.
_GEO_LABELS = frozenset({"parcel # / geo id", "geographic id", "geo id"})
_MAILING_LABELS = frozenset({"mailing address"})
_SITUS_LABELS = frozenset({"address", "situs address", "situs"})
_PROP_ID_LABELS = frozenset({"property id"})
# A deliverable first line: a house number, or a box / route / PMB form. Lines
# before it are addressees ("C/O ...", a trust name), which we do not collect.
_STREET_RE = re.compile(
    r"^(?:\d|P\.?\s*O\.?\s*BOX\b|POST OFFICE BOX\b|BOX\b|PMB\b|RR\b|HC\b|GENERAL DELIVERY\b)",
    re.I,
)
_LOCALITY_RE = re.compile(r"^(?P<city>.+?)\s*,?\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")


def is_pacs_url(url: str | None) -> bool:
    """Return True if url looks like a Tyler PACS PropertyAccess portal."""
    if not url:
        return False
    low = url.lower()
    return "/propertyaccess" in low or "propertyaccess/" in low


def normalize_pacs_parcel(value: object) -> str | None:
    """Digits only, leading zeros dropped; None for anything that is not a parcel.

    Parcel ids must stay STRINGS end to end; this key is only ever compared, never
    stored. Hyphens, spaces and dots are formatting ("0530084-000100000" and
    "0530084000100000" are the same Clallam parcel); leading zeros are stripped on
    BOTH sides of every comparison so a zero-padded recorder id still matches.
    """
    raw = re.sub(r"[\s.\-]", "", str(value or ""))
    if not raw.isdigit():
        return None
    return raw.lstrip("0") or None


def _cell_lines(td) -> list[str]:
    """A table cell's visible lines, split on <br>, blank lines dropped."""
    for br in td.find_all("br"):
        br.replace_with("\n")
    return [" ".join(ln.split()) for ln in td.get_text("\n").split("\n") if ln.strip()]


def parse_pacs_detail_html(html_text: str) -> dict:
    """The identity and address cells of a PACS Property.aspx page.

    Returns ``{"geo_ids": [...], "prop_ids": [...], "situs": [lines...] | None,
    "mailing": [[lines...], ...]}``. ``mailing`` has one line-list per owner block
    (co-owners each get a "Mailing Address:" cell); an owner block whose cell is
    empty contributes an empty list, so the caller can tell "the county prints no
    address" from "the page has no such label". Pure function, no HTTP.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    out: dict = {"geo_ids": [], "prop_ids": [], "situs": None, "mailing": []}
    for td in soup.find_all("td"):
        label = td.get_text(" ", strip=True).rstrip(":").strip().lower()
        if not label:
            continue
        value_td = td.find_next_sibling("td")
        if value_td is None:
            continue
        if label in _GEO_LABELS:
            lines = _cell_lines(value_td)
            if lines:
                out["geo_ids"].append(lines[0])
        elif label in _PROP_ID_LABELS:
            lines = _cell_lines(value_td)
            if lines:
                out["prop_ids"].append(lines[0])
        elif label in _MAILING_LABELS:
            out["mailing"].append(_cell_lines(value_td))
        elif label in _SITUS_LABELS and out["situs"] is None:
            out["situs"] = _cell_lines(value_td)
    return out


def compose_pacs_mailing(lines: list[str]) -> str | None:
    """One "Mailing Address:" cell -> the comma-joined string results.mailing_address holds.

    Addressee lines before the first deliverable line are dropped; a trailing
    "CITY, ST 99320[-1234]" locality is normalised to "CITY, ST ZIP"; anything the
    county printed in another shape (a foreign address, a bare locality) is kept
    as printed so no source information is destroyed. None when there is no
    deliverable line at all, which the caller treats as unparsed, never as "none".
    """
    lines = [ln.strip() for ln in lines if ln and ln.strip()]
    start = next((i for i, ln in enumerate(lines) if _STREET_RE.match(ln)), None)
    if start is None:
        return None
    parts = lines[start:]
    m = _LOCALITY_RE.match(parts[-1].upper()) if len(parts) >= 2 else None
    if m:
        parts[-1] = f"{m['city'].strip().rstrip(',')}, {m['state']} {m['zip']}"
    return ", ".join(parts)[:512]  # results.mailing_address is String(512)


def pacs_detail_url(pacs_url: str, prop_id: str) -> str:
    """The Property.aspx detail URL on the same portal (same origin, same cid)."""
    parsed = urlparse(pacs_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("PACS portal URL must be https")  # same origin, same scheme as the search
    cid = (parse_qs(parsed.query).get("cid") or ["0"])[0]
    if not cid.isdigit():
        raise ValueError("PACS cid must be numeric")
    base = pacs_url.split("?", 1)[0]
    if not base.endswith("/"):
        base = base.rsplit("/", 1)[0] + "/"
    return urljoin(base, f"Property.aspx?cid={cid}&prop_id={prop_id}")


def _norm_name(text: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9&' ]", " ", (text or "").upper()).split())


def owner_cell_matches(cell: str, owner_name: str) -> bool:
    """The grid's Owner Name cell names the searched owner: exactly, or as the first
    of co-owners ("QUERY & SPOUSE"). The portal search is a starts-with match, so a
    search for DOE JANE can return DOE JANET; that row is not an answer (Codex P1)."""
    cell_n, q = _norm_name(cell), _norm_name(owner_name)
    return bool(q) and (cell_n == q or cell_n.startswith(q + " &"))


def parse_pacs_result_html(html_text: str, owner_name: str | None = None) -> dict | None:
    """Parse a PACS PropertyAccess search-results page into {address, value, prop_id}.

    ``address`` is the grid's Property Address (the SITUS). The grid has no mailing
    column, so ``mailing`` is never returned from here; ``prop_id`` (the portal's
    own integer key, taken from the row's detail link) is what a caller uses to
    fetch the Property.aspx page where the labelled "Mailing Address:" lives.
    With ``owner_name`` the single row must also name that owner (see
    ``owner_cell_matches``); without a header naming the owner column, any cell
    may carry the name.

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
            candidate_rows.append((cells, row))
    if len(candidate_rows) != 1:
        return None
    cells, row_html = candidate_rows[0]
    if owner_name is not None and not any(owner_cell_matches(c, owner_name) for c in cells):
        # Column positions drift (empty cells are dropped above), so any cell may
        # carry the name; an exact normalized owner string cannot collide with an
        # address, legal or value cell.
        return None

    result: dict[str, str] = {}
    for cell in cells:
        cell_clean = cell.replace("\r\n", "\n").replace("\r", "\n")
        # The situs: number + street, possibly with city/state on the next line.
        # Only the first line is the street the lead is keyed on; the rest is the
        # situs locality, NOT a mailing address (see the module docstring).
        if re.search(r"\d+\s+[A-Z].*WA\s+\d{5}", cell_clean, re.I | re.DOTALL):
            lines = [ln.strip() for ln in cell_clean.split("\n") if ln.strip()]
            result["address"] = lines[0]
        elif cell.startswith("$") and "value" not in result:
            result["value"] = cell
    m = re.search(r"prop_id=(\d{1,12})", row_html, re.IGNORECASE)
    if m:
        result["prop_id"] = m.group(1)

    # parcel_id intentionally NOT extracted from owner-name search (see above).
    return result if result.get("address") else None


def mailing_from_detail(session: requests.Session, pacs_url: str, prop_id: str,
                        timeout: int = 20) -> str | None:
    """The labelled owner mailing address from one Property.aspx page, or None.

    None covers every non-answer (HTTP failure, no label, an empty cell, co-owner
    cells that disagree, an unparseable cell): on the owner-NAME path a missing
    mailing address is simply not filled, and nothing downstream distinguishes the
    reasons. The parcel-keyed path (pacs_parcel.py) does, and uses the parser directly.
    """
    try:
        r = session.get(pacs_detail_url(pacs_url, prop_id), timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        _logger.warning("PACS detail fetch failed: %s", type(exc).__name__)
        return None
    if r.status_code != 200:
        return None
    page = parse_pacs_detail_html(r.text)
    if page["prop_ids"] != [str(prop_id)]:
        # The page must be the record the grid row pointed at (Codex P1): a
        # redirect, a session page or a layout change is never somebody's address.
        return None
    blocks = page["mailing"]
    if not blocks or any(not lines for lines in blocks):
        return None  # no owner block, or a co-owner with no address: not an answer
    composed = {compose_pacs_mailing(lines) for lines in blocks}
    if None in composed:
        return None  # an owner block we cannot read is not a block we may ignore
    return composed.pop() if len(composed) == 1 else None


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
        # N1 + audit #5 5b-ii: a pinned session resolves the host once, refuses it
        # if any answer is a blocked address, and connects to the address it
        # checked, so a DNS answer that changes after validate_scraping_target
        # (rebinding) cannot reach an internal host. It also ignores ambient
        # HTTP(S)_PROXY. allow_redirects=False on both hops so a poisoned 302
        # can't bounce to an internal/metadata host; PACS posts back to the same
        # URL, so there is no legitimate redirect.
        sess = pinned_session()
        sess.headers.update(_HEADERS)
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

        result = parse_pacs_result_html(r.text, owner_name)
        if result and result.get("prop_id"):
            # The grid never carries the owner's mailing address; the detail page
            # does, under its own label. One more same-origin GET per unique hit.
            mailing = mailing_from_detail(sess, pacs_url, result.pop("prop_id"), timeout=_GET_TIMEOUT)
            if mailing:
                result["mailing"] = mailing
        elif result:
            result.pop("prop_id", None)
        return result
    except Exception as exc:
        # PII: owner_name is a third party who never signed up, and the log file has
        # no rotation or retention, so it is dropped. Only pacs_url and owner_name are
        # in scope here and there is no non-identifying record id to correlate on, so
        # the exception text is the diagnostic handle.
        _logger.warning("PACS name lookup failed: %s", str(exc)[:80])
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
