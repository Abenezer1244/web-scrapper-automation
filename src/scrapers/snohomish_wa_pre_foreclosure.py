"""Snohomish County (WA) — Pre-foreclosure (Notice of Trustee Sale) scraper.

Source: the Snohomish County Tribune weekly "Legals" PDF (Pacific Publishing). WA
law (RCW 61.24.040) requires every trustee sale to be published in a county legal
newspaper; the Snohomish County recorder (Hyland LandmarkWeb) BANS automated access
in its ToS, so — exactly as for Pierce (Tacoma Daily Index) — we use the newspaper.

This is the SAME Snohomish-specific PDF the nts_crawler harvests into the
nts_notices auction-data cache, but here it produces LEADS (Result rows): one
ScrapedRecord per Notice of Trustee Sale (grantor = the motivated owner). Because
it's the Snohomish Tribune (not a statewide aggregator), every notice is Snohomish
— no cross-county filtering needed. The matcher then attaches the cache's auction
fields onto these leads.

Pure HTTP (static-HTML listing → soft-404 with the link present; text-based PDF) —
overrides the Playwright lifecycle to no-ops like snohomish_wa_tax_delinquent.py.
Reuses the TESTED nts_pdf adapter (extract/normalize/split) + the shared
nts_tacoma_index.parse_nts_notice statutory parser. The discovery (find the current
PDF on the paper's page) is inlined to avoid a scrapers→workers import.
"""
import html as _html
import os
import re
import tempfile
from urllib.parse import urlparse

from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.preforeclosure import strip_vesting_clause
from src.scrapers.sources import nts_pdf
from src.scrapers.sources import nts_tacoma_index as nts
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_download_to_file, safe_get_following

_logger = setup_logger("scraper.snohomish_wa_pre_foreclosure")

# Discover the current weekly Legals PDF on the paper's legal-notices page. The page
# 3xx-redirects then soft-404s (404 with the links present), gates non-browser UAs
# with a 403, and lists TWO PDFs (a "CLASS …" classifieds PDF + the "Legals" one) —
# so we follow redirects, send a browser UA, don't gate on status, and keep the
# newest CDN link whose filename contains "legal". (Mirrors nts_crawler; kept inline
# so this scraper doesn't import worker code.)
_PAGE_URL = "https://www.snoho.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498"
_PDF_HOST = "pacificpublishingcompany.media.clients.ellingtoncms.com"
_PDF_PREFIX = "/static-4/snoho/images/"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"
)
_MAX_PDF_BYTES = 25 * 1024 * 1024
_SOURCE = "snohomish_tribune"
_HREF_PDF = re.compile(r'href="([^"]+\.pdf)"', re.I)


def _discover_pdf_url() -> str | None:
    try:
        resp = safe_get_following(
            _PAGE_URL, timeout=settings.DEFAULT_TIMEOUT, headers={"User-Agent": _BROWSER_UA}
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Snohomish NTS discovery failed: %s", str(exc)[:140])
        return None
    for raw in _HREF_PDF.findall(resp.text):
        url = _html.unescape(raw)
        p = urlparse(url)
        base = p.path.rsplit("/", 1)[-1].lower()
        if p.hostname == _PDF_HOST and p.path.startswith(_PDF_PREFIX) and "legal" in base:
            return url
    _logger.info("Snohomish NTS discovery: no legals PDF on %s (HTTP %d)", _PAGE_URL, resp.status_code)
    return None


def _record_from_notice(parsed: dict) -> ScrapedRecord:
    """Build a pre_foreclosure ScrapedRecord from a parsed, valid NTS notice."""
    rec = ScrapedRecord()
    # The NTS grantor line is the homeowner followed by legal vesting boilerplate
    # ("... A MARRIED MAN, AS HIS SOLE AND SEPARATE PROPERTY") — strip it so the lead
    # shows just the owner name(s). De-hyphenation already happened in normalize_pdf_text.
    rec.party_name = BridgeScraper.clean(strip_vesting_clause(parsed.get("grantor")))
    rec.property_address = parsed.get("property_address")
    rec.parcel_id = parsed.get("parcel")
    rec.doc_type = "Notice of Trustee Sale"
    # date_recorded feeds the date window + "new" badge; the NOD transmittal date is
    # the closest recording-like date, else fall back to the auction date.
    rec.date_recorded = parsed.get("nod_date") or parsed.get("auction_date")
    # Carry the auction fields for reference; Result.auction_date itself is populated
    # by the NTS matcher (county-scoped) attaching the nts_notices cache.
    rec.enrichment_data = {
        "source": _SOURCE,
        "county": "snohomish",
        "state": "WA",
        "ts_number": parsed.get("ts_number"),
        "auction_date": parsed.get("auction_date"),
        "auction_time": parsed.get("auction_time"),
        "trustee": parsed.get("trustee"),
        "beneficiary": parsed.get("beneficiary"),
        "default_amount": str(parsed["principal_owing"]) if parsed.get("principal_owing") else None,
    }
    return rec


class SnohomishWAPreForeclosureScraper(BridgeScraper):
    """Scrapes Notice-of-Trustee-Sale leads from the Snohomish County Tribune PDF."""

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — Snohomish publishes Notice-of-Trustee-Sale legals only."""
        from src.scrapers.doc_scope import CollectionScope, DocTypeItem

        if record_type != "pre_foreclosure":
            return None
        return CollectionScope(
            kind="document_type",
            items=(DocTypeItem(label="Notice of Trustee Sale", exact=True),),
            note="Published trustee-sale legal notices (Snohomish County Tribune).",
        )

    def __init__(self, record_type: str = "pre_foreclosure") -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # date_from/date_to are part of the connector interface but intentionally
        # unused (Codex review): this source is ONE current weekly publication — the
        # latest Tribune Legals PDF — NOT a searchable historical index, so there is
        # no per-record date range to satisfy (you cannot get a prior month's Snoho
        # NTS from this week's PDF). Mirrors snohomish_wa_tax_delinquent, the other
        # current-snapshot connector. Crucially, post-hoc filtering the returned
        # records by [date_from, date_to] would be WRONG here: a lead's date_recorded
        # is its FUTURE auction date, so a typical past-looking scrape window would
        # drop exactly the active trustee sales we want. Every notice this PDF carries
        # is a current, active sale; the user-facing date window lives at the
        # results-query / Lists layer (date_recorded_parsed), not at scrape-insert.
        del date_from, date_to

        pdf_url = _discover_pdf_url()
        if not pdf_url:
            raise RuntimeError("Snohomish Tribune: no current legals PDF found (page layout/source change)")
        _logger.info("Snohomish Tribune legals PDF → %s", pdf_url)

        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="snoho_nts_")
        os.close(fd)
        try:
            safe_download_to_file(
                pdf_url, path, max_bytes=_MAX_PDF_BYTES, require_https=True,
                headers={"User-Agent": _BROWSER_UA}, timeout=settings.DEFAULT_TIMEOUT,
            )
            with open(path, "rb") as fh:
                data = fh.read()
            text = nts_pdf.extract_pdf_text(data)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        blocks = nts_pdf.split_notice_blocks(nts_pdf.normalize_pdf_text(text))
        records: list[ScrapedRecord] = []
        for block in blocks:
            parsed = nts.parse_nts_notice(block)
            if not nts.is_valid_nts(parsed):
                continue  # commercial/MTC/other format we don't yet parse — safely skipped
            records.append(_record_from_notice(parsed))

        if not records:
            # The dominant Quality Loan / North Star formats parse; a week with zero
            # parseable NTS is plausible (small paper) but worth surfacing, not crashing.
            _logger.warning("Snohomish Tribune: %d notice blocks, 0 parseable NTS leads", len(blocks))
        _logger.info("Snohomish pre_foreclosure complete — %d blocks → %d leads", len(blocks), len(records))
        if self.on_progress:
            self.on_progress(1, 1, len(records))
        return records

    async def __aenter__(self) -> "SnohomishWAPreForeclosureScraper":
        return self

    async def __aexit__(self, *args) -> None:
        pass
