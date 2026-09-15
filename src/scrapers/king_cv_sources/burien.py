"""Burien code enforcement cases from the city's Cityworks case layer (ArcGIS Enterprise).

Source: gis.burienwa.gov cwpll_caseactivity MapServer layer 0, CaseType 'Code Enforcement'.
Each case prints the King County PIN (SiteParcelNumber), so parcel_id is set at scrape.
The layer has no owner; the owner is read from King eRealProperty for the PIN.

Quirks verified live on 2026-09-14:
  * AppliedDate is TEXT "MM/DD/YYYY", so the server cannot range-filter or sort it. The
    query selects the window's calendar years with LIKE '%/YYYY' and the exact days are
    kept here after parsing.
  * OBJECTID is NOT unique (103 distinct values across 108 cases in 2026), so paging
    orders by OBJECTID then CaseNumber to make the order total, and cases are
    de-duplicated by CaseNumber.
  * CaseAddress comes in several shapes ("1822 SW 152ND ST,  BURIEN,  98166",
    "646 SW 139TH ST,  BURIEN,  WA,  98166", "11848 12th Ave S Burien, WA 98168",
    "13007 12th Ave SW"). Every case is inside Burien city limits, so the city is Burien.
  * Data lags about two weeks behind today.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import ScrapedRecord
from src.scrapers.king_cv_sources import BURIEN
from src.scrapers.king_cv_sources.base import (
    CodeViolationSource,
    arcgis_query_all,
    normalize_king_pin,
)
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_cv_sources.burien")

_HOST = "gis.burienwa.gov"
_QUERY_URL = f"https://{_HOST}/server/rest/services/cwpll/cwpll_caseactivity/MapServer/0/query"
add_scrape_domain(_HOST)

_OUT_FIELDS = "OBJECTID,CaseID,SiteParcelNumber,CaseNumber,CaseType,CaseStatus,AppliedDate,CaseAddress"
# Well under the layer's maxRecordCount (4000); about 250 cases a year.
_PAGE_SIZE = 2000
_DATE_RE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$")
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_TRAILING_CITY_RE = re.compile(r"\s+BURIEN$", re.IGNORECASE)


def _parse_date(value: object) -> date | None:
    m = _DATE_RE.match(str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def normalize_address(raw: object) -> str | None:
    """"STREET, Burien WA ZIP" (ZIP only when the source printed one), else None."""
    parts = [" ".join(p.split()) for p in str(raw or "").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    street = _TRAILING_CITY_RE.sub("", parts[0]).strip()
    if not street or _ZIP_RE.fullmatch(street):
        return None
    zip_match = _ZIP_RE.search(parts[-1]) if len(parts) > 1 else None
    return f"{street}, Burien WA {zip_match.group(1)}" if zip_match else f"{street}, Burien WA"


class BurienSource(CodeViolationSource):
    key = BURIEN
    jurisdiction = "Burien"

    async def fetch(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y").date()
        end = datetime.strptime(date_to, "%m/%d/%Y").date()
        years = " OR ".join(f"AppliedDate LIKE '%/{y}'" for y in range(start.year, end.year + 1))
        where = f"CaseType='Code Enforcement' AND ({years})"
        _logger.info("Burien code enforcement %s to %s", date_from, date_to)
        rows = arcgis_query_all(
            _QUERY_URL,
            {"where": where, "outFields": _OUT_FIELDS, "returnGeometry": "false",
             "orderByFields": "OBJECTID ASC,CaseNumber ASC"},
            page_size=_PAGE_SIZE, what=f"{self.key} query", on_page=self._progress)

        records: list[ScrapedRecord] = []
        seen: set[str] = set()
        parsed = 0
        for attrs in rows:
            case = " ".join(str(attrs.get("CaseNumber") or "").split())
            applied = _parse_date(attrs.get("AppliedDate"))
            if not case or applied is None:
                continue
            parsed += 1
            if case in seen or not (start <= applied <= end):
                continue
            seen.add(case)
            raw_pin = attrs.get("SiteParcelNumber")
            case_id = attrs.get("CaseID")
            record = ScrapedRecord(
                date_recorded=applied.strftime("%m/%d/%Y"),
                party_name=None,
                legal_description=case,
                parcel_id=normalize_king_pin(raw_pin),
                property_address=normalize_address(attrs.get("CaseAddress")),
                raw_html_hash=hashlib.sha256(f"{self.key}|{case}".encode()).hexdigest()[:32],
            )
            record.enrichment_data = {
                "source": self.key,
                "case_number": case,
                # Cityworks' numeric id, for looking the case up with the city.
                "case_id": int(case_id) if isinstance(case_id, (int, float)) else None,
                "status": attrs.get("CaseStatus"),
                # Burien publishes no violation category.
                "violation_category": None,
                "case_type": attrs.get("CaseType"),
                "applied_date": attrs.get("AppliedDate"),
                "source_parcel_number": raw_pin,
            }
            records.append(record)
        self.check_canary(len(rows), parsed)
        _logger.info("Burien code enforcement: %d cases from %d rows", len(records), len(rows))
        return records
